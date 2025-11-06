import pyaudio
import threading
import queue
import logging
from typing import Optional, Callable


class AudioHandler:
    """Audio recorder and player using PyAudio.\n    Provides input capture and low-latency playback with a lock-protected buffer.\n    """

    def __init__(
        self,
        sample_rate: int = 24000,
        chunk_size: int = 480,
        channels: int = 1,
        format: int = pyaudio.paInt16,
    ) -> None:
        # Config
        self.sample_rate = sample_rate
        self.chunk_size = chunk_size
        self.channels = channels
        self.format = format

        # PyAudio
        self.audio = pyaudio.PyAudio()

        # Recording
        self.recording = False
        self.record_thread: Optional[threading.Thread] = None
        self.audio_queue = queue.Queue()
        self.input_stream: Optional[pyaudio.Stream] = None

        # Playback
        self.playing = False
        self.play_thread: Optional[threading.Thread] = None
        self.playback_buffer = bytearray()
        self._pb_lock = threading.Lock()
        self.playback_queue = queue.Queue()
        self.output_stream: Optional[pyaudio.Stream] = None

        # Small preroll to avoid underflow at start
        self.preroll_ms: float = 120.0
        self._primed: bool = False

        # Callbacks
        self.on_audio_data: Optional[Callable] = None

        # Logger
        self.logger = logging.getLogger(__name__)

        # Open streams
        self._init_streams()

    def _init_streams(self) -> None:
        try:
            # Input
            self.input_stream = self.audio.open(
                format=self.format,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                frames_per_buffer=self.chunk_size,
                stream_callback=self._input_callback,
            )

            # Output (slightly larger buffer to reduce underflows)
            self.output_stream = self.audio.open(
                format=self.format,
                channels=self.channels,
                rate=self.sample_rate,
                output=True,
                frames_per_buffer=self.chunk_size * 2,
                stream_callback=self._output_callback,
            )

            self.logger.info("Audio streams initialized")
        except Exception as e:
            self.logger.error(f"Failed to init audio streams: {e}")
            raise

    def _input_callback(self, in_data, frame_count, time_info, status):
        if self.recording:
            self.audio_queue.put(in_data)
        return (None, pyaudio.paContinue)

    def _output_callback(self, in_data, frame_count, time_info, status):
        need = frame_count * self.channels * 2  # bytes for int16
        with self._pb_lock:
            bytes_len = len(self.playback_buffer)
            backlog_ms = 1000.0 * bytes_len / (self.sample_rate * self.channels * 2)

            # preroll: output silence until enough data buffered
            if not self._primed:
                if backlog_ms >= self.preroll_ms:
                    self._primed = True
                else:
                    return (b"\x00" * need, pyaudio.paContinue)

            if bytes_len >= need:
                out = bytes(self.playback_buffer[:need])
                del self.playback_buffer[:need]
            elif bytes_len > 0:
                out = bytes(self.playback_buffer)
                self.playback_buffer.clear()
                out += b"\x00" * (need - len(out))
            else:
                out = b"\x00" * need
        return (out, pyaudio.paContinue)

    def playback_backlog_ms(self) -> float:
        with self._pb_lock:
            bytes_len = len(self.playback_buffer)
        return 1000.0 * bytes_len / (self.sample_rate * self.channels * 2)

    def start_recording(self) -> None:
        if self.recording:
            self.logger.warning("Recording already active")
            return
        self.recording = True
        self.record_thread = threading.Thread(target=self._record_worker, daemon=True)
        self.record_thread.start()
        if not self.input_stream.is_active():
            self.input_stream.start_stream()
        self.logger.info("Recording started")

    def stop_recording(self) -> None:
        if not self.recording:
            return
        self.recording = False
        if self.input_stream.is_active():
            self.input_stream.stop_stream()
        if self.record_thread:
            self.record_thread.join(timeout=1.0)
        self.logger.info("Recording stopped")

    def _record_worker(self) -> None:
        while self.recording:
            try:
                if not self.audio_queue.empty():
                    audio_data = self.audio_queue.get(timeout=0.1)
                    if self.on_audio_data:
                        self.on_audio_data(audio_data)
            except queue.Empty:
                continue
            except Exception as e:
                self.logger.error(f"Record worker error: {e}")
                break

    def start_playback(self) -> None:
        if self.playing:
            return
        self.playing = True
        self._primed = False
        if not self.output_stream.is_active():
            self.output_stream.start_stream()
        self.logger.info("Playback started")

    def stop_playback(self) -> None:
        if not self.playing:
            return
        self.playing = False
        while not self.playback_queue.empty():
            try:
                self.playback_queue.get_nowait()
            except queue.Empty:
                break
        if self.output_stream.is_active():
            self.output_stream.stop_stream()
        self.logger.info("Playback stopped")

    def play_audio(self, audio_data: bytes) -> None:
        if not self.playing:
            self.start_playback()
        with self._pb_lock:
            self.playback_buffer.extend(audio_data)
    def clear_playback_buffer(self) -> None:
        """Clear any buffered TTS bytes to avoid overlap on new responses."""
        with self._pb_lock:
            self.playback_buffer.clear()
            self._primed = False
    def get_audio_devices(self):
        devices = []
        for i in range(self.audio.get_device_count()):
            di = self.audio.get_device_info_by_index(i)
            devices.append(
                {
                    "index": i,
                    "name": di["name"],
                    "max_input_channels": di["maxInputChannels"],
                    "max_output_channels": di["maxOutputChannels"],
                    "default_sample_rate": di["defaultSampleRate"],
                }
            )
        return devices

    def set_input_device(self, device_index: int) -> None:
        try:
            if self.input_stream:
                self.input_stream.close()
            self.input_stream = self.audio.open(
                format=self.format,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                input_device_index=device_index,
                frames_per_buffer=self.chunk_size,
                stream_callback=self._input_callback,
            )
            self.logger.info(f"Input device set to {device_index}")
        except Exception as e:
            self.logger.error(f"Failed to set input device: {e}")
            raise

    def set_output_device(self, device_index: int) -> None:
        try:
            if self.output_stream:
                self.output_stream.close()
            self.output_stream = self.audio.open(
                format=self.format,
                channels=self.channels,
                rate=self.sample_rate,
                output=True,
                output_device_index=device_index,
                frames_per_buffer=self.chunk_size * 2,
                stream_callback=self._output_callback,
            )
            self.logger.info(f"Output device set to {device_index}")
        except Exception as e:
            self.logger.error(f"Failed to set output device: {e}")
            raise

    def cleanup(self) -> None:
        self.stop_recording()
        self.stop_playback()
        if self.input_stream:
            self.input_stream.close()
        if self.output_stream:
            self.output_stream.close()
        self.audio.terminate()
        self.logger.info("Audio handler cleaned up")
