import asyncio
import base64
from audio.microphone import MicrophoneInput
from audio.speaker import SpeakerOutput
from providers.gemini_live import GeminiLiveProvider

class VoiceSession:
    def __init__(self):
        self.mic = MicrophoneInput()
        self.speaker = SpeakerOutput()
        self.provider = GeminiLiveProvider()
        self._running = False
        self.send_task = None
        self.receive_task = None

    async def run(self):
        print("Connecting to Gemini Live API...", flush=True)
        await self.provider.connect()
        print("Connected! Initializing audio hardware...", flush=True)

        self.mic.start()
        self.speaker.start()
        self._running = True

        self.send_task = asyncio.create_task(self._send_loop())
        self.receive_task = asyncio.create_task(self._receive_loop())

        print("\n>>> Gemini Live session active. Speak now! Press Ctrl+C to exit. <<<\n", flush=True)

        try:
            # Wait for either task to complete (e.g. connection close or exception)
            done, pending = await asyncio.wait(
                [self.send_task, self.receive_task],
                return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def _send_loop(self):
        try:
            while self._running:
                # Capture next chunk from mic
                chunk = await self.mic.get_chunk()
                # Stream it to Gemini
                await self.provider.send_audio(chunk)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"\nError in audio capture/send loop: {e}", flush=True)

    async def _receive_loop(self):
        try:
            is_model_speaking = False
            async for response in self.provider.receive():
                if not self._running:
                    break

                tool_call = response.get("toolCall")
                if tool_call:
                    names = [
                        function_call.get("name", "<unknown>")
                        for function_call in tool_call.get("functionCalls", [])
                    ]
                    print(f"\n[Tool Call] {', '.join(names)}", flush=True)
                    await self.provider.send_tool_response(tool_call)
                    continue

                # Extract serverContent payload (camelCase in raw WebSockets)
                server_content = response.get("serverContent")
                if not server_content:
                    continue

                # Handle model interruption / barge-in
                if server_content.get("interrupted"):
                    print("\n[Interrupted by User] Flushing speaker queue...\n", flush=True)
                    self.speaker.clear()
                    is_model_speaking = False
                    continue

                # Print what the user said (input transcription)
                input_transcription = server_content.get("inputTranscription")
                if input_transcription:
                    user_text = input_transcription.get("text")
                    if user_text:
                        print(f"\nUser: {user_text}", flush=True)

                # Print what the model is saying (output transcription)
                output_transcription = server_content.get("outputTranscription")
                if output_transcription:
                    model_text = output_transcription.get("text")
                    if model_text:
                        if not is_model_speaking:
                            print("Gemini: ", end="", flush=True)
                            is_model_speaking = True
                        print(model_text, end="", flush=True)

                # Process returned model audio content
                model_turn = server_content.get("modelTurn")
                if model_turn:
                    for part in model_turn.get("parts", []):
                        # Play back returned PCM audio
                        inline_data = part.get("inlineData")
                        if inline_data and inline_data.get("data"):
                            # Decoded base64 string to raw PCM bytes
                            audio_bytes = base64.b64decode(inline_data.get("data"))
                            await self.speaker.play(audio_bytes)
                        
                        # Print fallback text transcript (if any text modality returned and outputTranscription wasn't used)
                        if part.get("text") and not output_transcription:
                            if not is_model_speaking:
                                print("Gemini: ", end="", flush=True)
                                is_model_speaking = True
                            print(part.get("text"), end="", flush=True)

                # End of model's turn
                if server_content.get("turnComplete"):
                    print("", flush=True)
                    is_model_speaking = False

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"\nError in audio playback/receive loop: {e}", flush=True)

    async def stop(self):
        self._running = False
        print("\nShutting down audio streams...", flush=True)
        
        if self.send_task:
            self.send_task.cancel()
        if self.receive_task:
            self.receive_task.cancel()

        # Stop microphone and speaker
        try:
            self.mic.stop()
        except Exception:
            pass

        try:
            self.speaker.stop()
        except Exception:
            pass

        # Disconnect WebSocket session
        try:
            await self.provider.disconnect()
        except Exception:
            pass
            
        print("Shutdown complete. Goodbye!", flush=True)
