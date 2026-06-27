import asyncio
import pyaudio
import config

class MicrophoneInput:
    def __init__(self):
        self.p = pyaudio.PyAudio()
        self.stream = None
        self.queue = asyncio.Queue()
        self.loop = None

    def start(self):
        self.loop = asyncio.get_running_loop()
        
        try:
            device_info = self.p.get_default_input_device_info()
            print(f"[Audio] Recording from mic: {device_info.get('name')} (Index: {device_info.get('index')})", flush=True)
        except Exception as e:
            print(f"[Audio] Warning: Could not retrieve default input device info: {e}", flush=True)

        def callback(in_data, frame_count, time_info, status):
            if in_data:
                # Put data into the queue safely from the PortAudio C thread
                self.loop.call_soon_threadsafe(self.queue.put_nowait, in_data)
            return (None, pyaudio.paContinue)

        self.stream = self.p.open(
            format=pyaudio.paInt16,
            channels=config.CHANNELS,
            rate=config.INPUT_SAMPLE_RATE,
            input=True,
            frames_per_buffer=config.CHUNK_SIZE,
            stream_callback=callback
        )
        self.stream.start_stream()

    async def get_chunk(self):
        return await self.queue.get()

    def stop(self):
        if self.stream:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception:
                pass
        self.p.terminate()
