#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenAI Realtime voice assistant (UTF-8, no mojibake).
"""

import asyncio
import logging
import signal
import sys
from typing import Optional
import time
import struct
import math
import os


def rms16(b: bytes) -> int:
    if not b:
        return 0
    samples = struct.iter_unpack("<h", b)
    n = len(b) // 2
    acc = 0
    for (s,) in samples:
        acc += s * s
    return int(math.sqrt(acc / n)) if n else 0


from app.clients.realtime_client import RealtimeClient
from .audio_handler import AudioHandler
from app.clients.prompt_runner import render_system_instruction
from app.realtime.context import make_session_instructions


class VoiceAssistant:
    """Realtime Voice Assistant"""

    def __init__(self):
        self.realtime_client: Optional[RealtimeClient] = None
        self.audio_handler: Optional[AudioHandler] = None
        self.running = False
        self.logger = logging.getLogger(__name__)
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.audio_queue: Optional[asyncio.Queue] = None
        self.sender_task: Optional[asyncio.Task] = None

        # VAD/interrupt related
        self.noise_rms = 200
        self.last_cancel_ts = 0.0
        self._rx_buf = bytearray()
        self._last_tts_rx_ts = 0.0
        self._speech_above_gate_since = 0.0

        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

        # Mic sensitivity / echo controls (env-tunable)
        # Larger multipliers => less sensitive to background/echo
        self.vad_gate_multiplier = float(os.getenv("VAD_GATE_MULTIPLIER", "3.0"))  # default 3.0 (was ~2.5)
        self.strict_gate_multiplier = float(os.getenv("VAD_STRICT_GATE_MULTIPLIER", "3.0"))  # default 3.0 (was 2.2)
        self.strict_gate_min = int(os.getenv("VAD_STRICT_GATE_MIN", "2000"))  # default 2000 (was 1800)
        # Grace period after TTS audio before allowing mic
        self.grace_window_s = float(os.getenv("TTS_GRACE_WINDOW_S", "1.2"))  # default 1.2s (was 0.8s)
        # How long speech must exceed strict gate while TTS is playing
        self.speech_sustain_s = float(os.getenv("SPEECH_SUSTAIN_S", "0.75"))  # default 0.75s (was 0.50s)
        # If true, enforce strict half-duplex: when TTS plays, drop mic frames
        self.half_duplex_strict = (os.getenv("HALF_DUPLEX_STRICT", "false").lower() in ("1", "true", "yes", "y"))

    async def initialize(self):
        """Init components"""
        try:
            # Realtime client
            self.realtime_client = RealtimeClient()

            # System instructions (mirrors user language via system prompt)
            try:
                sys_file = os.getenv("REALTIME_SYSTEM_FILE", "system_global.txt")
                base_instr = render_system_instruction(sys_file)


                pid_env = os.getenv("REALTIME_PATIENT_ID", "").strip()
                if pid_env.isdigit():
                    try:
                        full_instr = await make_session_instructions(int(pid_env), sys_file)
                        # Apply full session instructions (system + patient snapshot)
                        self.realtime_client.session_config["session"]["instructions"] = full_instr
                    except Exception:
                        pass
                else:
                    # Apply base system prompt if no patient context
                    self.realtime_client.session_config["session"]["instructions"] = base_instr
            except Exception:
                pass

            # Event handlers
            self.realtime_client.on_audio_response = self.handle_audio_response
            self.realtime_client.on_text_response = self.handle_text_response
            self.realtime_client.on_error = self.handle_error
            # Response lifecycle hooks to avoid overlap/duplication
            self.realtime_client.on_response_started = self._on_response_started
            self.realtime_client.on_response_finished = self._on_response_finished

            # Audio
            self.audio_handler = AudioHandler(sample_rate=24000, chunk_size=480, channels=1)
            self.audio_handler.on_audio_data = self.handle_audio_input

            # Bridge to asyncio
            self.loop = asyncio.get_running_loop()
            self.audio_queue = asyncio.Queue(maxsize=30)
            self.logger.info("Components initialized")

        except Exception as e:
            self.logger.error(f"init failed: {e}")
            raise

    async def handle_audio_response(self, audio_data: bytes):
        if not self.audio_handler:
            return
        # mark last TTS receive time for grace window
        self._last_tts_rx_ts = time.monotonic()
        self._rx_buf.extend(audio_data)
        FRAME_20MS_BYTES = 960  # 24kHz * 2 bytes * 20ms
        while len(self._rx_buf) >= FRAME_20MS_BYTES:
            chunk = bytes(self._rx_buf[:FRAME_20MS_BYTES])
            del self._rx_buf[:FRAME_20MS_BYTES]
            self.audio_handler.play_audio(chunk)

    async def handle_text_response(self, text: str):
        print(f"AI: {text}", end='', flush=True)

    async def handle_error(self, error_message: str):
        self.logger.error(f"error: {error_message}")

    async def _on_response_started(self):
        # Clear any stale TTS audio when a new response begins
        try:
            if self.audio_handler:
                self.audio_handler.clear_playback_buffer()
        except Exception:
            pass

    async def _on_response_finished(self):
        # Placeholder for future metrics/state updates
        return

    def handle_audio_input(self, audio_data: bytes):
        if not (self.audio_queue and self.loop):
            return

        def _enqueue():
            # 1) energy
            rms = rms16(audio_data)

            # 2) playback backlog
            backlog_ms = 0.0
            if self.audio_handler:
                backlog_ms = self.audio_handler.playback_backlog_ms()

            # 3) adaptive gate from noise baseline
            if backlog_ms <= 0:
                self.noise_rms = 0.95 * self.noise_rms + 0.05 * rms
            gate = max(500, int(self.noise_rms * self.vad_gate_multiplier))

            # 3.5) when playing: strict half-duplex and cancel policy
            now = time.monotonic()
            if backlog_ms > 0:
                # Optional strict half-duplex: block mic while TTS playing
                if self.half_duplex_strict:
                    self._speech_above_gate_since = 0.0
                    return

                # grace window after last TTS audio
                if (now - self._last_tts_rx_ts) < self.grace_window_s:
                    self._speech_above_gate_since = 0.0
                    return

                strict_gate = max(int(gate * self.strict_gate_multiplier), self.strict_gate_min)
                if rms >= strict_gate:
                    if self._speech_above_gate_since == 0.0:
                        self._speech_above_gate_since = now
                    sustained = (now - self._speech_above_gate_since) >= self.speech_sustain_s
                    if sustained and (now - self.last_cancel_ts) > 0.60:
                        rc = self.realtime_client
                        if rc and rc.is_connected and (rc.active_resp_id or rc.pending_create):
                            asyncio.create_task(rc.cancel_response())
                            self.last_cancel_ts = now
                            self._speech_above_gate_since = 0.0
                    # allow high-energy frames to flow to server VAD
                else:
                    # drop likely echo/noise
                    self._speech_above_gate_since = 0.0
                    return

            # 4) not playing: enqueue normally
            try:
                self.audio_queue.put_nowait(audio_data)
            except asyncio.QueueFull:
                try:
                    _ = self.audio_queue.get_nowait()
                except Exception:
                    pass
                try:
                    self.audio_queue.put_nowait(audio_data)
                except Exception:
                    pass

        self.loop.call_soon_threadsafe(_enqueue)

    async def _audio_sender(self):
        assert self.audio_queue is not None
        while self.running:
            chunk = await self.audio_queue.get()
            try:
                if self.realtime_client and self.realtime_client.is_connected:
                    await self.realtime_client.send_audio(chunk)
            except Exception as e:
                self.logger.error(f"send audio failed: {e}")

    async def start(self):
        try:
            self.running = True
            self.logger.info("Connecting to OpenAI Realtime API...")
            await self.realtime_client.connect()
            await asyncio.sleep(2)

            # start playback (warm up output)
            self.audio_handler.start_playback()

            await self.realtime_client.create_response_with_instructions(
                "Hello, I'm a medical service assistant. How can I help you?",
                modalities=("audio", "text"),
            )
            await asyncio.sleep(3)

            self.audio_handler.start_recording()
            self.sender_task = asyncio.create_task(self._audio_sender())
            self.logger.info("Voice assistant started. You can speak now...")
            self.logger.info("Press Ctrl+C to exit")

            while self.running:
                await asyncio.sleep(0.1)

        except KeyboardInterrupt:
            self.logger.info("received exit signal")
        except Exception as e:
            self.logger.error(f"runtime error: {e}")
        finally:
            await self.cleanup()

    async def cleanup(self):
        self.running = False
        if self.sender_task:
            self.sender_task.cancel()
            try:
                await self.sender_task
            except asyncio.CancelledError:
                pass
        if self.audio_handler:
            self.audio_handler.cleanup()
        if self.realtime_client:
            await self.realtime_client.disconnect()
        self.logger.info("cleanup done")

    def show_audio_devices(self):
        
        if not self.audio_handler:
            print("Audio handler not initialized")
        devices = self.audio_handler.get_audio_devices()
        devices = self.audio_handler.get_audio_devices()
        print("\nAvailable audio devices")

        for device in devices:
            print(f"Device {device['index']}: {device['name']}")
            print(f"Device {device['index']}: {device['name']}")
            print(f"  Input channels: {device['max_input_channels']}")
            print(f"  Output channels: {device['max_output_channels']}")
            print(f"  Default sample rate: {device['default_sample_rate']}")

def signal_handler(signum, frame):
    print("\nExiting...")
    sys.exit(0)


async def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    assistant = VoiceAssistant()
    try:
        await assistant.initialize()
        assistant.show_audio_devices()

        print("Change audio devices? (y/n): ", end="")
        choice = input().strip().lower()
        if choice == "y":
            print("Input device index (Enter to skip): ", end="")
            input_device = input().strip()
            if input_device.isdigit():
                assistant.audio_handler.set_input_device(int(input_device))

            print("Output device index (Enter to skip): ", end="")
            output_device = input().strip()
            if output_device.isdigit():
                assistant.audio_handler.set_output_device(int(output_device))

        await assistant.start()

    except Exception as e:
        logging.error(f"program error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProgram exited")
    except Exception as e:
        print(f"program error: {e}")
        sys.exit(1)



