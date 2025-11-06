# app/routes/realtime_openai.py
# ------------------------------------------------------------
# PURPOSE
#   Issue a short-lived OpenAI Realtime *client token* to the browser.
#   - The browser cannot hold your OPENAI_API_KEY.
#   - It calls this route to get an ephemeral token that is valid only
#     briefly (OpenAI enforces TTL). The browser then uses this token
#     to negotiate a WebRTC session directly with OpenAI Realtime.
#
# WHAT THIS IS NOT
#   - Not the token for calling our /tools/* endpoints. That’s your
#     /realtime/tool-token route we already built.
#
# FLOW
#   Frontend →
#     POST /realtime/openai-token { model?, doctor_id?, session_id? }
#       → server POST https://api.openai.com/v1/realtime/sessions (with our OPENAI_API_KEY)
#       → returns {"client_secret":{"value":"..."}}
#
# SECURITY
#   Keep OPENAI_API_KEY server-side. Do NOT cache this token long-term
#   on the client—treat as ephemeral per session/tab.
# ------------------------------------------------------------

from __future__ import annotations

import os
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.utils.prompt_loader import load_prompt

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "..", "prompts", "realtime_voice_intake.md")
PROMPT_TEXT = load_prompt(PROMPT_PATH)

router = APIRouter()

# --- Config --------------------------------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set in the environment")

# Choose your default Realtime model.
# You can override per request if needed.
DEFAULT_REALTIME_MODEL = os.getenv(
    "REALTIME_MODEL",
    "gpt-4o-realtime-preview-2024-12-17"
)
# Optional defaults for the “feel” of the realtime session.
# You can keep defaults now; tune later after first listen.
DEFAULT_VOICE = os.getenv("REALTIME_VOICE", "verse")  # e.g., 'verse', 'alloy', 'aria', etc.

# --- Request schema -----------------------------------------

class RealtimeTokenRequest(BaseModel):
    model: str = Field(default=DEFAULT_REALTIME_MODEL, description="Realtime model name")
    doctor_id: Optional[str] = Field(default=None, description="(Optional) For observability")
    session_id: Optional[str] = Field(default=None, description="(Optional) Link to our summary session")
    # If you want to expose voice control from the client, you can add:
    voice: Optional[str] = Field(default=None, description="Override TTS voice for this session")
# --- Route ---------------------------------------------------

@router.post("/realtime/openai-token")
async def issue_openai_realtime_token(body: RealtimeTokenRequest):
    """
    Server-side call to OpenAI to mint a short-lived client token that the browser
    will use to open a WebRTC session directly with OpenAI Realtime.

    Returns:
      { "client_secret": { "value": "<ephemeral token>" }, "id": "...", ... }
    """
    # Endpoint to create a Realtime session token
    url = "https://api.openai.com/v1/realtime/sessions"

    # Build minimal payload.
    # - voice: keep simple now; you can expand later (latency_mode, barge-in, etc.)
    payload = {
    "model": body.model,

    # TTS voice for model replies
    "voice": body.voice or DEFAULT_VOICE,

    # 🔑 Make the session auto-transcribe incoming mic audio
    "input_audio_transcription": {
        "model": "whisper-1"     # low-latency ASR for realtime
    },

    # Let the model speak + text back
    "modalities": ["audio", "text"],

    # Remote audio codec from model -> browser
    "output_audio_format": "pcm16",

    # 🔧 Server VAD so the model auto-responds at your pauses
    "turn_detection": {
        "type": "server_vad",
        "threshold": 0.4,           # a bit more sensitive
        "prefix_padding_ms": 300,
        "silence_duration_ms": 1100, # wait ~1.1s of silence
        "create_response": True,     # auto create response when VAD fires
        "interrupt_response": True
    },

    # System instructions for the voice persona
    "instructions": PROMPT_TEXT
    }



    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            # Propagate OpenAI error back to client for debugging
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        js = resp.json()

        # Expect shape: { "client_secret": { "value": "..." }, "id": "...", ... }
        cs = js.get("client_secret", {})
        if not isinstance(cs, dict) or "value" not in cs:
            raise HTTPException(status_code=500, detail=f"Unexpected OpenAI response: {js}")

        return js

    except HTTPException:
        # Keep FastAPI HTTPException as-is
        raise
    except Exception as e:
        # Wrap any other failure
        raise HTTPException(status_code=500, detail=f"Failed to mint realtime token: {e}")
    


