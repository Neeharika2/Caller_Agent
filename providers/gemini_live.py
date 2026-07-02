import json
import base64
import websockets
import config
from runtime.tools import default_tool_registry

class GeminiLiveProvider:
    def __init__(self, tool_registry=default_tool_registry):
        self.websocket = None
        self.tool_registry = tool_registry

    async def connect(self):
        # Format raw WebSocket URL with API key
        url = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={config.GEMINI_API_KEY}"
        
        # Standardize model string
        model_name = config.MODEL
        if not model_name.startswith("models/"):
            model_name = f"models/{model_name}"

        # Connect directly with ping configurations to avoid timeouts
        self.websocket = await websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20
        )

        # Build initial setup client message
        setup_msg = {
            "setup": {
                "model": model_name,
                "generationConfig": {
                    "responseModalities": ["AUDIO"]
                },
                "systemInstruction": {
                    "parts": [
                        {
                            "text": "You are a helpful, snappy voice assistant. Keep replies short and natural. If the user asks for the current time, use the get_current_time tool before answering. If the user asks about business details, products, services, policies, pricing, availability, or support information, use the search_knowledge tool before answering."
                        }
                    ]
                },
                "tools": self.tool_registry.as_gemini_tools(),
                "inputAudioTranscription": {},
                "outputAudioTranscription": {}
            }
        }

        # Send handshake configuration
        await self.websocket.send(json.dumps(setup_msg))

        # Wait for the setupComplete server message
        response_str = await self.websocket.recv()
        response = json.loads(response_str)
        if "setupComplete" not in response:
            raise RuntimeError(f"WebSocket handshake failed. Expected setupComplete, got: {response}")

    async def send_audio(self, chunk: bytes):
        if self.websocket:
            # Base64 encode raw PCM chunks and send via realtimeInput schema
            base64_data = base64.b64encode(chunk).decode("utf-8")
            msg = {
                "realtimeInput": {
                    "audio": {
                        "mimeType": "audio/pcm;rate=16000",
                        "data": base64_data
                    }
                }
            }
            await self.websocket.send(json.dumps(msg))

    async def receive(self):
        if self.websocket:
            try:
                # Continuously yield parsed JSON response dicts from the socket
                async for message_str in self.websocket:
                    yield json.loads(message_str)
            except websockets.exceptions.ConnectionClosed:
                pass

    async def send_tool_response(self, tool_call: dict):
        if self.websocket:
            response_msg = await self.tool_registry.build_tool_response(tool_call)
            await self.websocket.send(json.dumps(response_msg))

    async def disconnect(self):
        if self.websocket:
            try:
                await self.websocket.close()
            except Exception:
                pass
            self.websocket = None
