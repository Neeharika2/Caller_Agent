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

app = FastAPI()

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
    exotel_sample_rate = int(sample_rate_param)
    print(f"[Exotel] Caller connected to /media. Negotiated sample rate: {exotel_sample_rate}Hz", flush=True)

    stream_sid = None

    # Connect directly to Gemini Live API without client-side keepalive ping to avoid timeout issues
    try:
        async with websockets.connect(GEMINI_WS_URL, ping_interval=None) as gemini_ws:
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
                                "text": "You are a helpful, snappy telephone voice assistant. Speak in short, concise sentences. Respond immediately without long preambles. Avoid bullet points, lists, markdown formatting, or lengthy explanations."
                            }
                        ]
                    },
                    "inputAudioTranscription": {},
                    "outputAudioTranscription": {}
                }
            }
            await gemini_ws.send(json.dumps(setup_msg))

            # Receive setup complete
            setup_complete_str = await gemini_ws.recv()
            print(f"[Gemini] Setup Complete: {setup_complete_str}", flush=True)

            # Send initial greeting trigger turn to make Gemini speak first as soon as call connects
            initial_greeting = {
                "clientContent": {
                    "turns": [
                        {
                            "role": "user",
                            "parts": [
                                {
                                    "text": "The user has just answered the phone call. Greet them warmly and ask how you can help them today in a single short sentence."
                                }
                            ]
                        }
                    ],
                    "turnComplete": True
                }
            }
            await gemini_ws.send(json.dumps(initial_greeting))

            last_user_audio_time = 0.0
            is_model_responding = False
            turn_complete_sent = False

            async def silence_vad():
                nonlocal last_user_audio_time, is_model_responding, turn_complete_sent
                SILENCE_MS = 600
                CHECK_INTERVAL = 0.2
                while True:
                    await asyncio.sleep(CHECK_INTERVAL)
                    if last_user_audio_time > 0 and not is_model_responding and not turn_complete_sent:
                        elapsed = (time.time() - last_user_audio_time) * 1000
                        if elapsed > SILENCE_MS:
                            turn_msg = {
                                "clientContent": {
                                    "turns": [],
                                    "turnComplete": True
                                }
                            }
                            await gemini_ws.send(json.dumps(turn_msg))
                            turn_complete_sent = True

            async def exotel_to_gemini():
                nonlocal stream_sid, last_user_audio_time, is_model_responding, turn_complete_sent
                media_packets_recv = 0
                inbound_state = None  # Tracks resampling state across chunks
                try:
                    async for message in exotel_ws.iter_text():
                        data = json.loads(message)
                        event = data.get("event")

                        if event == "start":
                            stream_sid = data.get("stream_sid") or data.get("start", {}).get("stream_sid")
                            print(f"[Exotel] Stream started. Stream SID: {stream_sid}", flush=True)

                        elif event == "media":
                            media_packets_recv += 1
                            last_user_audio_time = time.time()
                            turn_complete_sent = False
                            
                            if media_packets_recv == 1:
                                print("[Exotel] First media packet received from caller!", flush=True)
                            elif media_packets_recv % 100 == 0:
                                print(f"[Exotel] Received {media_packets_recv} media packets from caller.", flush=True)

                            media_data = data.get("media", {})
                            payload = media_data.get("payload")
                            if payload:
                                # Decode the raw PCM audio payload
                                audio_raw = base64.b64decode(payload)
                                
                                # Resample to 16kHz for Gemini if not already 16kHz
                                if exotel_sample_rate != 16000:
                                    audio_16khz, inbound_state = audioop.ratecv(
                                        audio_raw, 2, 1, exotel_sample_rate, 16000, inbound_state
                                    )
                                else:
                                    audio_16khz = audio_raw
                                
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
                nonlocal stream_sid, last_user_audio_time, is_model_responding, turn_complete_sent
                media_packets_sent = 0
                outbound_state = None  # Tracks resampling state across chunks
                try:
                    async for message_str in gemini_ws:
                        response = json.loads(message_str)
                        server_content = response.get("serverContent")
                        if not server_content:
                            continue

                        # Handle Barge-In / Interruption
                        if server_content.get("interrupted"):
                            print("[Gemini] User interrupted Gemini speaking. Clearing playback buffer...", flush=True)
                            is_model_responding = False
                            turn_complete_sent = False
                            if stream_sid and exotel_ws.client_state == WebSocketState.CONNECTED:
                                clear_msg = {
                                    "event": "clear",
                                    "stream_sid": stream_sid
                                }
                                await exotel_ws.send_text(json.dumps(clear_msg))
                            continue

                        # Handle Turn Complete from Gemini
                        if server_content.get("turnComplete"):
                            is_model_responding = False
                            turn_complete_sent = False
                            continue

                        # Log output transcript if present
                        output_transcription = server_content.get("outputTranscription")
                        if output_transcription and output_transcription.get("text"):
                            print(f"[Gemini Transcription] {output_transcription.get('text')}", flush=True)

                        model_turn = server_content.get("modelTurn")
                        if model_turn:
                            # Log latency when Gemini starts responding to a turn
                            if not is_model_responding:
                                is_model_responding = True
                                turn_complete_sent = False
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
                                        audio_out, outbound_state = audioop.ratecv(
                                            audio_24khz, 2, 1, 24000, exotel_sample_rate, outbound_state
                                        )
                                    else:
                                        audio_out = audio_24khz
                                    
                                    # Log resampling details
                                    print(f"[Debug Audio Out] Gemini chunk size: {len(audio_24khz)} bytes -> Resampled ({exotel_sample_rate}Hz): {len(audio_out)} bytes", flush=True)
                                    
                                    # Send back to Exotel
                                    if stream_sid and exotel_ws.client_state == WebSocketState.CONNECTED:
                                        media_packets_sent += 1
                                        if media_packets_sent == 1:
                                            print("[Gemini -> Exotel] First audio packet sent to caller!", flush=True)
                                        elif media_packets_sent % 100 == 0:
                                            print(f"[Gemini -> Exotel] Sent {media_packets_sent} audio packets to caller.", flush=True)
                                            
                                        exotel_payload = {
                                            "event": "media",
                                            "stream_sid": stream_sid,
                                            "media": {
                                                "payload": base64.b64encode(audio_out).decode("utf-8")
                                            }
                                        }
                                        await exotel_ws.send_text(json.dumps(exotel_payload))
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    import traceback
                    print(f"[Error] Exception in Gemini -> Exotel loop: {e}", flush=True)
                    traceback.print_exc()

            # Concurrent execution of stream loops + silence VAD
            await asyncio.gather(exotel_to_gemini(), gemini_to_exotel(), silence_vad())

    except Exception as e:
        import traceback
        print(f"[Error] WebSocket bridge error: {e}", flush=True)
        traceback.print_exc()
    finally:
        print("[Bridge] Closed call WebSocket session.", flush=True)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
