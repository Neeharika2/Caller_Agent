import os
import json
import base64
import asyncio
import audioop
import time
from fastapi import FastAPI, WebSocket
from fastapi.websockets import WebSocketState
import uvicorn
import websockets
import config
from runtime.tools import default_tool_registry

app = FastAPI()

EXOTEL_MIN_CHUNK_BYTES = 3200
EXOTEL_CHUNK_MULTIPLE_BYTES = 320
GEMINI_INPUT_CHUNK_MS = 40
GEMINI_VAD_SILENCE_MS = 500


def parse_sample_rate(value, default=8000):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# Standardize Gemini Model Name
model_name = config.MODEL
if not model_name.startswith("models/"):
    model_name = f"models/{model_name}"

GEMINI_WS_URL = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={config.GEMINI_API_KEY}"


@app.websocket("/media")
async def websocket_endpoint(exotel_ws: WebSocket):
    await exotel_ws.accept()
    
    # Parse negotiated sample rate from query parameter (default to 8000Hz)
    sample_rate_param = exotel_ws.query_params.get("sample-rate", "8000")
    exotel_sample_rate = parse_sample_rate(sample_rate_param)
    print(f"[Exotel] Caller connected to /media. URL sample rate hint: {exotel_sample_rate}Hz", flush=True)

    stream_sid = None

    # Connect to Gemini Live API with keepalive ping to avoid timeout issues
    try:
        async with websockets.connect(GEMINI_WS_URL, ping_interval=20, ping_timeout=20) as gemini_ws:
            print("[Gemini] Connected to Gemini Live API. Initializing handshake...", flush=True)
            
            # Setup session with system instructions optimized for snappy telephone voice bots
            setup_msg = {
                "setup": {
                    "model": model_name,
                    "generationConfig": {
                        "responseModalities": ["AUDIO"]
                    },
                    "systemInstruction": {
                        "parts": [
                            {
                                "text": "You are a helpful, snappy telephone voice assistant. Detect the caller's language from their speech and reply in the same language. If the caller switches languages, switch with them. For Indian callers, comfortably support Hindi, English, Hinglish, and regional languages. Keep replies short and natural. Respond immediately without long preambles. Avoid bullet points, lists, markdown formatting, or lengthy explanations. If the caller asks for the current time, use the get_current_time tool before answering."
                            }
                        ]
                    },
                    "tools": default_tool_registry.as_gemini_tools(),
                    "realtimeInputConfig": {
                        "automaticActivityDetection": {
                            "disabled": False,
                            "startOfSpeechSensitivity": "START_SENSITIVITY_HIGH",
                            "endOfSpeechSensitivity": "END_SENSITIVITY_HIGH",
                            "prefixPaddingMs": 20,
                            "silenceDurationMs": GEMINI_VAD_SILENCE_MS
                        },
                        "activityHandling": "START_OF_ACTIVITY_INTERRUPTS"
                    },
                    "inputAudioTranscription": {},
                    "outputAudioTranscription": {}
                }
            }
            await gemini_ws.send(json.dumps(setup_msg))

            # Receive setup complete
            setup_complete_str = await gemini_ws.recv()
            print(f"[Gemini] Setup Complete: {setup_complete_str}", flush=True)

            last_user_audio_time = 0.0
            is_model_responding = False

            async def exotel_to_gemini():
                nonlocal stream_sid, exotel_sample_rate, last_user_audio_time
                media_packets_recv = 0
                inbound_state = None  # Tracks resampling state across chunks
                audio_buffer = bytearray()
                
                buffer_threshold = int(exotel_sample_rate * 2 * (GEMINI_INPUT_CHUNK_MS / 1000))
                
                try:
                    async for message in exotel_ws.iter_text():
                        data = json.loads(message)
                        event = data.get("event")

                        if event == "start":
                            stream_sid = data.get("stream_sid") or data.get("start", {}).get("stream_sid")
                            print(f"[Exotel] Stream started. Stream SID: {stream_sid}", flush=True)
                            media_format = data.get("start", {}).get("media_format") or data.get("start", {}).get("mediaFormat")
                            if media_format:
                                print(f"[Exotel] Start media format: {media_format}", flush=True)
                                start_sample_rate = parse_sample_rate(
                                    media_format.get("sample_rate") or media_format.get("sampleRate"),
                                    exotel_sample_rate
                                )
                                if start_sample_rate != exotel_sample_rate:
                                    exotel_sample_rate = start_sample_rate
                                    buffer_threshold = int(exotel_sample_rate * 2 * (GEMINI_INPUT_CHUNK_MS / 1000))
                                    inbound_state = None
                                    print(f"[Exotel] Using start-event sample rate: {exotel_sample_rate}Hz", flush=True)

                            # Send initial greeting trigger turn to make Gemini speak first as soon as call connects
                            initial_greeting = {
                                "clientContent": {
                                    "turns": [
                                        {
                                            "role": "user",
                                            "parts": [
                                                {
                                                    "text": "The user has just answered the phone call. Greet them warmly in a neutral, simple way and ask how you can help in one short sentence. After the caller speaks, use the caller's language."
                                                }
                                            ]
                                        }
                                    ],
                                    "turnComplete": True
                                }
                            }
                            await gemini_ws.send(json.dumps(initial_greeting))
                            print("[Gemini] Sent initial greeting trigger turn.", flush=True)

                        elif event == "media":
                            media_packets_recv += 1
                            if media_packets_recv == 1:
                                print("[Exotel] First media packet received from caller!", flush=True)
                            elif media_packets_recv % 100 == 0:
                                print(f"[Exotel] Received {media_packets_recv} media packets from caller.", flush=True)

                            media_data = data.get("media", {})
                            payload = media_data.get("payload")
                            if payload:
                                # Exotel VoiceBot uses raw signed 16-bit little-endian PCM,
                                # even at 8kHz. Do not decode as PCMU/mu-law.
                                audio_raw = base64.b64decode(payload)
                                
                                # Voice Activity Detection (VAD) via RMS threshold
                                rms = audioop.rms(audio_raw, 2)
                                if rms > 800:
                                    last_user_audio_time = time.time()
                                
                                # Buffer the 16-bit PCM audio to reduce processing/resampling per-packet overhead
                                audio_buffer.extend(audio_raw)
                                
                                if len(audio_buffer) >= buffer_threshold:
                                    chunk_to_send = bytes(audio_buffer[:buffer_threshold])
                                    del audio_buffer[:buffer_threshold]
                                    
                                    # Resample to 16kHz for Gemini if not already 16kHz
                                    if exotel_sample_rate != 16000:
                                        audio_16khz, inbound_state = audioop.ratecv(
                                            chunk_to_send, 2, 1, exotel_sample_rate, 16000, inbound_state
                                        )
                                    else:
                                        audio_16khz = chunk_to_send
                                    
                                    # Package and send to Gemini
                                    gemini_msg = {
                                        "realtimeInput": {
                                            "audio": {
                                                "mimeType": "audio/pcm;rate=16000",
                                                "data": base64.b64encode(audio_16khz).decode("utf-8")
                                            }
                                        }
                                    }
                                    await gemini_ws.send(json.dumps(gemini_msg))

                        elif event == "stop":
                            print(f"[Exotel] Stream stopped for Stream SID: {stream_sid}", flush=True)
                            break
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    import traceback
                    print(f"[Error] Exception in Exotel -> Gemini loop: {e}", flush=True)
                    traceback.print_exc()

            async def gemini_to_exotel():
                nonlocal stream_sid, last_user_audio_time, is_model_responding
                media_packets_sent = 0
                outbound_state = None  # Tracks resampling state across chunks
                outbound_buffer = bytearray()
                outbound_timestamp_ms = 0

                async def send_exotel_audio(force=False):
                    nonlocal media_packets_sent, outbound_timestamp_ms
                    if not stream_sid or exotel_ws.client_state != WebSocketState.CONNECTED:
                        return

                    while len(outbound_buffer) >= EXOTEL_MIN_CHUNK_BYTES:
                        chunk = bytes(outbound_buffer[:EXOTEL_MIN_CHUNK_BYTES])
                        del outbound_buffer[:EXOTEL_MIN_CHUNK_BYTES]
                        media_packets_sent += 1
                        if media_packets_sent == 1:
                            print("[Gemini -> Exotel] First audio packet sent to caller!", flush=True)
                        elif media_packets_sent % 100 == 0:
                            print(f"[Gemini -> Exotel] Sent {media_packets_sent} audio packets to caller.", flush=True)

                        exotel_payload = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": base64.b64encode(chunk).decode("utf-8")
                            }
                        }
                        outbound_timestamp_ms += int((len(chunk) / 2) / exotel_sample_rate * 1000)
                        await exotel_ws.send_text(json.dumps(exotel_payload))

                    if force and outbound_buffer:
                        remainder_len = len(outbound_buffer)
                        padded_len = max(
                            EXOTEL_MIN_CHUNK_BYTES,
                            ((remainder_len + EXOTEL_CHUNK_MULTIPLE_BYTES - 1) // EXOTEL_CHUNK_MULTIPLE_BYTES)
                            * EXOTEL_CHUNK_MULTIPLE_BYTES
                        )
                        outbound_buffer.extend(b"\x00" * (padded_len - remainder_len))
                        await send_exotel_audio(force=False)

                try:
                    async for message_str in gemini_ws:
                        response = json.loads(message_str)
                        tool_call = response.get("toolCall")
                        if tool_call:
                            names = [
                                function_call.get("name", "<unknown>")
                                for function_call in tool_call.get("functionCalls", [])
                            ]
                            print(f"[Gemini Tool Call] {', '.join(names)}", flush=True)
                            tool_response = await default_tool_registry.build_tool_response(tool_call)
                            await gemini_ws.send(json.dumps(tool_response))
                            continue

                        server_content = response.get("serverContent")
                        if not server_content:
                            continue

                        # Handle Barge-In / Interruption
                        if server_content.get("interrupted"):
                            print("[Gemini] User interrupted Gemini speaking. Clearing playback buffer...", flush=True)
                            is_model_responding = False
                            last_user_audio_time = 0.0
                            if stream_sid and exotel_ws.client_state == WebSocketState.CONNECTED:
                                clear_msg = {
                                    "event": "clear",
                                    "streamSid": stream_sid
                                }
                                await exotel_ws.send_text(json.dumps(clear_msg))
                            continue

                        # Handle Turn Complete from Gemini
                        if server_content.get("turnComplete"):
                            await send_exotel_audio(force=True)
                            is_model_responding = False
                            last_user_audio_time = 0.0
                            continue

                        # Log input transcript if present. This proves caller audio reached Gemini.
                        input_transcription = server_content.get("inputTranscription")
                        if input_transcription and input_transcription.get("text"):
                            print(f"[Gemini Input Transcription] {input_transcription.get('text')}", flush=True)

                        # Log output transcript if present
                        output_transcription = server_content.get("outputTranscription")
                        if output_transcription and output_transcription.get("text"):
                            print(f"[Gemini Transcription] {output_transcription.get('text')}", flush=True)

                        model_turn = server_content.get("modelTurn")
                        if model_turn:
                            # Log latency when Gemini starts responding to a turn
                            if not is_model_responding:
                                is_model_responding = True
                                if last_user_audio_time > 0.0:
                                    latency_ms = int((time.time() - last_user_audio_time) * 1000)
                                    print(f"[Latency] Time to first response packet: {latency_ms}ms", flush=True)

                            for part in model_turn.get("parts", []):
                                if part.get("text"):
                                    print(f"[Gemini Text Part] {part.get('text')}", flush=True)
                                    
                                inline_data = part.get("inlineData")
                                if inline_data and inline_data.get("data"):
                                    # Decode Gemini's 24kHz audio output
                                    audio_24khz = base64.b64decode(inline_data["data"])
                                    
                                    # Resample from 24kHz to Exotel's negotiated sample rate
                                    if exotel_sample_rate != 24000:
                                        audio_out_pcm, outbound_state = audioop.ratecv(
                                            audio_24khz, 2, 1, 24000, exotel_sample_rate, outbound_state
                                        )
                                    else:
                                        audio_out_pcm = audio_24khz
                                    
                                    # Log resampling details
                                    print(f"[Debug Audio Out] Gemini chunk size: {len(audio_24khz)} bytes -> PCM ({exotel_sample_rate}Hz): {len(audio_out_pcm)} bytes", flush=True)
                                    
                                    # Exotel expects raw linear PCM chunks, base64 encoded.
                                    outbound_buffer.extend(audio_out_pcm)
                                    await send_exotel_audio()
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    import traceback
                    print(f"[Error] Exception in Gemini -> Exotel loop: {e}", flush=True)
                    traceback.print_exc()

            # Concurrent execution of stream loops + silence VAD
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(exotel_to_gemini()),
                    asyncio.create_task(gemini_to_exotel())
                ],
                return_when=asyncio.FIRST_COMPLETED
            )
            # Cancel all pending tasks to prevent coroutine / memory leaks
            for task in pending:
                task.cancel()

    except Exception as e:
        import traceback
        print(f"[Error] WebSocket bridge error: {e}", flush=True)
        traceback.print_exc()
    finally:
        print("[Bridge] Closed call WebSocket session.", flush=True)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
