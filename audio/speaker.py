import queue
import threading
import pyaudio
import config

class SpeakerOutput:
    def __init__(self):
        self.p = pyaudio.PyAudio()
        self.stream = None
        self.queue = queue.Queue()
        self.buffer = bytearray()
        self.lock = threading.Lock()

    def start(self):
        try:
            device_info = self.p.get_default_output_device_info()
            print(f"[Audio] Playing to speaker: {device_info.get('name')} (Index: {device_info.get('index')})", flush=True)
        except Exception as e:
            print(f"[Audio] Warning: Could not retrieve default output device info: {e}", flush=True)

        def callback(in_data, frame_count, time_info, status):
            requested_bytes = frame_count * 2
            
            with self.lock:
                # Pull from queue to fill the buffer if we don't have enough bytes
                while len(self.buffer) < requested_bytes:
                    try:
                        chunk = self.queue.get_nowait()
                        if chunk:
                            self.buffer.extend(chunk)
                    except queue.Empty:
                        break
                
                # Extract requested_bytes from buffer
                if len(self.buffer) >= requested_bytes:
                    data = bytes(self.buffer[:requested_bytes])
                    del self.buffer[:requested_bytes]
                else:
                    data_len = len(self.buffer)
                    data = bytes(self.buffer) + b'\x00' * (requested_bytes - data_len)
                    self.buffer.clear()
                
            return (data, pyaudio.paContinue)

        self.stream = self.p.open(
            format=pyaudio.paInt16,
            channels=config.CHANNELS,
            rate=config.OUTPUT_SAMPLE_RATE,
            output=True,
            frames_per_buffer=config.CHUNK_SIZE,
            stream_callback=callback
        )
        self.stream.start_stream()

    async def play(self, data: bytes):
        self.queue.put(data)

    def clear(self):
        """Immediately clear the playback queue and buffer."""
        with self.lock:
            # Drain queue
            while not self.queue.empty():
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    break
            # Clear buffer
            self.buffer.clear()

    def stop(self):
        if self.stream:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception:
                pass
        self.p.terminate()
