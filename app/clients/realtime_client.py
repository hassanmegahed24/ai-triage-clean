import asyncio
import websockets
import json
import base64
import logging
from typing import Optional, Callable
from dotenv import load_dotenv
import os

# Load environment variables
load_dotenv()


class RealtimeClient:
    """OpenAI Realtime API WebSocket client (UTF-8 clean)."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required")

        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.is_connected = False
        # When True, create a response automatically after server VAD detects speech stop
        self.auto_create_on_silence: bool = True
        # Optional language preference from env (e.g., 'zh', 'zh-CN', 'en', 'es')
        default_lang = (os.getenv("REALTIME_DEFAULT_LANGUAGE") or "").strip()

        # Server-side VAD sensitivity (higher threshold => less sensitive)
        vad_threshold = float(os.getenv("REALTIME_VAD_THRESHOLD", "0.85"))  # default more strict than 0.75
        vad_prefix_ms = int(os.getenv("REALTIME_VAD_PREFIX_MS", "300"))
        vad_silence_ms = int(os.getenv("REALTIME_VAD_SILENCE_MS", "1200"))

        self.session_config = {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                # Language mirroring is defined in the app's system prompt
                "instructions": (
                    "You are a medical AI assistant. Keep speaking in the user's language and ask follow-up questions until next steps are clear."
                    + (
                        f" Unless asked otherwise, reply in {default_lang}."
                        if default_lang
                        else ""
                    )
                ),
                "voice": "alloy",
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                # If a default language is provided, pass it to Whisper to improve
                # language detection and reduce accidental language switches.
                "input_audio_transcription": (
                    {"model": "whisper-1", "language": default_lang}
                    if default_lang
                    else {"model": "whisper-1"}
                ),
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": vad_threshold,
                    "prefix_padding_ms": vad_prefix_ms,
                    "silence_duration_ms": vad_silence_ms,
                },
                "tools": [],
                "tool_choice": "auto",
                # Lower temp to 0.6 to reduce off-script openings
                "temperature": 0.6,
                "max_response_output_tokens": "inf",
            },
        }

        # Callback hooks
        self.on_audio_response: Optional[Callable] = None
        self.on_text_response: Optional[Callable] = None
        self.on_error: Optional[Callable] = None
        self.on_speech_started: Optional[Callable] = None
        self.on_speech_stopped: Optional[Callable] = None
        self.on_response_started: Optional[Callable] = None
        self.on_response_finished: Optional[Callable] = None
        self.on_user_transcript: Optional[Callable] = None  # recognized local mic text

        # Response state tracking
        self.active_resp_id: Optional[str] = None
        self.pending_create: bool = False
        self.has_active_response: bool = False

        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(__name__)

    async def connect(self):
        """Connect to OpenAI Realtime API."""
        try:
            url = (
                "wss://api.openai.com/v1/realtime?model="
                "gpt-4o-realtime-preview-2024-10-01"
            )
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "OpenAI-Beta": "realtime=v1",
            }

            self.websocket = await websockets.connect(url, extra_headers=headers)
            self.is_connected = True
            self.logger.info("Connected to OpenAI Realtime API")

            await self.send_session_update()

            # Async listen loop
            asyncio.create_task(self.listen_for_messages())

        except Exception as e:
            self.logger.error(f"connect failed: {e}")
            if self.on_error:
                await self.on_error(f"connect failed: {e}")

    async def cancel_response(self):
        if self.websocket and self.active_resp_id:
            await self.websocket.send(json.dumps({"type": "response.cancel"}))
            self.logger.info("requested response.cancel")

    async def send_session_update(self):
        """Send session.update with current config."""
        if self.websocket:
            await self.websocket.send(json.dumps(self.session_config))
            self.logger.info("session.update sent")

    async def send_audio(self, audio_data: bytes):
        """Append audio bytes to the input buffer."""
        if not self.is_connected or not self.websocket:
            raise RuntimeError("not connected")
        audio_base64 = base64.b64encode(audio_data).decode("utf-8")
        message = {"type": "input_audio_buffer.append", "audio": audio_base64}
        await self.websocket.send(json.dumps(message))

    async def commit_audio(self):
        """Commit the input audio buffer (end of utterance)."""
        if not self.is_connected or not self.websocket:
            raise RuntimeError("not connected")
        await self.websocket.send(json.dumps({"type": "input_audio_buffer.commit"}))
        self.logger.info("input_audio_buffer.commit sent")

    async def create_response(self):
        """Create a response without extra instructions."""
        if not self.is_connected or not self.websocket:
            raise RuntimeError("not connected")
        if self.has_active_response or self.active_resp_id or self.pending_create:
            self.logger.info("response already in progress; skip create")
            return
        self.pending_create = True
        self.has_active_response = True
        await self.websocket.send(json.dumps({"type": "response.create"}))
        self.logger.info("response.create sent")

    async def create_response_with_instructions(
        self, text: str, modalities=("audio", "text")
    ):
        """Create a response with explicit instructions and modalities."""
        if not self.is_connected or not self.websocket:
            raise RuntimeError("not connected")
        if self.has_active_response or self.active_resp_id or self.pending_create:
            self.logger.info("response in progress; skip instruction create")
            return
        self.pending_create = True
        self.has_active_response = True
        payload = {
            "type": "response.create",
            "response": {"instructions": text, "modalities": list(modalities)},
        }
        await self.websocket.send(json.dumps(payload))
        self.logger.info("response.create (with instructions) sent")

    async def listen_for_messages(self):
        """Receive and dispatch messages from the server."""
        try:
            assert self.websocket is not None
            async for message in self.websocket:
                await self.handle_message(json.loads(message))
        except websockets.exceptions.ConnectionClosed:
            self.logger.info("WebSocket closed")
            self.is_connected = False
        except Exception as e:
            self.logger.error(f"listen error: {e}")
            if self.on_error:
                await self.on_error(f"listen error: {e}")

    async def handle_message(self, message: dict):
        """Handle a single server message."""
        message_type = message.get("type")

        if message_type == "session.created":
            self.logger.info("session.created")

        elif message_type == "session.updated":
            self.logger.info("session.updated")

        elif message_type == "input_audio_buffer.committed":
            self.logger.info("input_audio_buffer.committed")

        elif message_type == "input_audio_buffer.speech_started":
            self.logger.info("speech_started")
            if self.on_speech_started:
                try:
                    await self.on_speech_started()
                except Exception:
                    pass

        elif message_type == "input_audio_buffer.speech_stopped":
            self.logger.info("speech_stopped")
            if self.on_speech_stopped:
                try:
                    await self.on_speech_stopped()
                except Exception:
                    pass
            # Only auto-create when enabled; can be disabled by the server upon confirmation/finalize
            if self.auto_create_on_silence:
                await self.create_response()

        # Heuristic: forward Whisper recognition results to caller if present
        elif (isinstance(message_type, str) and "transcription" in message_type.lower()):
            # Try to extract best-effort transcript field
            text = (
                message.get("transcript")
                or message.get("text")
                or (message.get("delta") if isinstance(message.get("delta"), str) else None)
            )
            if text and self.on_user_transcript:
                try:
                    await self.on_user_transcript(text)
                except Exception:
                    pass

        elif message_type == "response.created":
            resp = message.get("response") or {}
            self.active_resp_id = resp.get("id")
            self.pending_create = False
            self.has_active_response = True
            if self.on_response_started:
                try:
                    await self.on_response_started()
                except Exception:
                    pass
            self.logger.info("response.created")

        elif message_type == "response.output_item.added":
            self.logger.info("response.output_item.added")

        elif message_type == "response.content_part.added":
            self.logger.info("response.content_part.added")

        elif message_type == "response.audio.delta":
            audio_data = message.get("delta")
            if audio_data and self.on_audio_response:
                audio_bytes = base64.b64decode(audio_data)
                await self.on_audio_response(audio_bytes)

        elif message_type == "response.text.delta":
            text_data = message.get("delta")
            if text_data and self.on_text_response:
                await self.on_text_response(text_data)

        elif message_type in (
            "response.completed",
            "response.canceled",
            "response.failed",
            "response.done",
        ):
            if self.on_response_finished:
                try:
                    await self.on_response_finished()
                except Exception:
                    pass
            self.active_resp_id = None
            self.pending_create = False
            self.has_active_response = False
            self.logger.info(message_type)

        elif message_type == "error":
            error_msg = message.get("error", {}).get("message", "unknown error")
            if isinstance(error_msg, str) and "active response in progress" in error_msg:
                self.has_active_response = True
                self.logger.warning(f"ignored concurrent create: {error_msg}")
            elif isinstance(error_msg, str) and "Cancellation failed: no active response found" in error_msg:
                self.active_resp_id = None
                self.pending_create = False
                self.has_active_response = False
                self.logger.info("ignore cancel failure: no active response (race)")
            else:
                self.logger.error(f"server error: {error_msg}")
                if self.on_error:
                    await self.on_error(f"server error: {error_msg}")

        else:
            self.logger.debug(f"unhandled message type: {message_type}")

    async def disconnect(self):
        """Close the websocket connection."""
        if self.websocket:
            await self.websocket.close()
            self.is_connected = False
            self.logger.info("disconnected")
