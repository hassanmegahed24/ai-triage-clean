# app/services/realtime_tool_handlers.py
from typing import Dict, Any, Optional, List, Iterable
import json
import re
from app.models.summary import MessageTurn
from app.clients.reasoning_client import ReasoningClient
from app.services.summary_session import (
    get_session,
    create_session,
    set_working_notes,
    get_working_notes,
)

# -----------------------------------------------------------------------------
# In-memory notes store (MVP)
# -----------------------------------------------------------------------------
NOTES_BY_SESSION: Dict[str, str] = {}
_MAX_NOTES_LEN = 12000
reason_client = ReasoningClient()


def _coerce_notes_input(raw: Any) -> str:
    """
    Accept strings, dicts, lists, or other JSON-friendly payloads and produce
    a human-readable string for storage/display. This guards against models
    sending structured objects instead of plain text.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        parts: List[str] = []
        for key, value in raw.items():
            if value is None or value == "":
                continue
            label = str(key).replace("_", " ").capitalize()
            if isinstance(value, (dict, list)):
                pretty = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
            else:
                pretty = str(value)
            parts.append(f"{label}: {pretty}")
        return "\n".join(parts)
    if isinstance(raw, Iterable) and not isinstance(raw, (bytes, bytearray)):
        pieces = []
        for item in raw:
            text = _coerce_notes_input(item)
            if text:
                pieces.append(text)
        return "\n".join(pieces)
    return str(raw)


def _cap(text: str) -> str:
    if len(text) > _MAX_NOTES_LEN:
        return text[-_MAX_NOTES_LEN:]
    return text


def _normalize(text: str) -> str:
    """
    Collapse runs of whitespace to single spaces, but keep paragraph breaks.
    Turns 'word\nword\nword' into 'word word word', while preserving blank lines.
    """
    if text is None:
        return ""
    paragraphs = re.split(r"\n\s*\n", text.strip())  # split on blank lines
    cleaned = []
    for p in paragraphs:
        cleaned.append(re.sub(r"\s+", " ", p.strip()))  # collapse internal whitespace
    return "\n\n".join([c for c in cleaned if c])


def get_notes(session_id: str) -> str:
    return NOTES_BY_SESSION.get(session_id, "")


def set_notes(session_id: str, notes: str) -> Dict[str, Any]:
    txt = _normalize(_coerce_notes_input(notes))
    NOTES_BY_SESSION[session_id] = _cap(txt)
    return {
        "ok": True,
        "session_id": session_id,
        "len": len(NOTES_BY_SESSION[session_id]),
        "message": "Notes overwritten.",
    }


def append_notes(session_id: str, delta: str) -> Dict[str, Any]:
    if not delta:
        return {"ok": True, "session_id": session_id, "len": len(get_notes(session_id))}
    existing = get_notes(session_id)
    chunk = _normalize(delta or "")
    # If no existing, just set; else add a space/newline as needed
    joiner = "\n" if ("\n" in existing or "\n" in chunk) else " "
    new_val = (existing + (joiner if existing else "") + chunk).strip()
    NOTES_BY_SESSION[session_id] = _cap(new_val)
    return {
        "ok": True,
        "session_id": session_id,
        "len": len(NOTES_BY_SESSION[session_id]),
        "message": "Notes appended.",
    }


# -----------------------------------------------------------------------------
# WS-registered tool names (used by the Realtime WS bridge)
# -----------------------------------------------------------------------------
async def save_observation(session_id: str, notes: str) -> Dict[str, Any]:
    """Persist (overwrite) the observation/notes for this session (MVP: memory)."""
    normalized_input = _coerce_notes_input(notes)
    print(f"[OBS] save_observation called session={session_id} notes_len={len(normalized_input)}")
    data = set_notes(session_id, normalized_input)
    normalized = NOTES_BY_SESSION.get(session_id, "")
    try:
        set_working_notes(session_id, normalized)
    except KeyError:
        # Initialize a minimal summary session so future calls have context.
        create_session(
            session_id=session_id,
            patient_id=0,
            doctor_id=None,
            consent=True,
            locale="en",
            snapshot={},
        )
        set_working_notes(session_id, normalized)
    payload = dict(data)
    payload["notes"] = normalized
    payload.setdefault("observation", normalized)
    return payload


async def finalize_soap(session_id: str) -> Dict[str, Any]:
    """Return a SOAP draft derived from the current notes (MVP stub)."""
    try:
        sess = get_session(session_id)
    except KeyError:
        # Fallback: bootstrap a minimal session so finalize can proceed.
        notes = get_notes(session_id)
        summary = create_session(
            session_id=session_id,
            patient_id=0,
            doctor_id=None,
            consent=True,
            locale="en",
            snapshot={},
        )
        if notes:
            set_working_notes(session_id, notes)
        sess = summary

    turns: List[MessageTurn] = list(sess.turns)
    if not turns:
        notes = get_working_notes(session_id) or get_notes(session_id)
        if notes:
            turns = [
                MessageTurn(role="assistant", content=notes, modality="text")
            ]

    try:
        print(f"[FINALIZE] calling reasoning for session={session_id}")
        result = await reason_client.generate_summary_finalize(
            turns=turns or [],
            snapshot=sess.snapshot or {},
            locale=sess.locale or "en",
            preview_only=True,
        )
        print(f"[FINALIZE] received keys={list(result.keys())}")
    except Exception as e:
        return {
            "ok": False,
            "message": f"Finalization failed: {e}",
            "session_id": session_id,
        }

    payload = {
        "ok": True,
        "session_id": session_id,
        "soap": result.get("soap"),
        "speech_output": result.get("speech_output"),
        "confidence": result.get("confidence"),
        "suggested_actions": result.get("suggested_actions"),
        "message": result.get("message", "SOAP draft ready."),
    }
    return payload


# -----------------------------------------------------------------------------
# HTTP tool endpoints call these async handlers (exactly what tools.py imports)
# -----------------------------------------------------------------------------
async def handle_summary_reply(
    payload: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    latest_user_text: Optional[str] = None,
    locale: Optional[str] = None,
    mode: Optional[str] = None,
    **kwargs,
) -> Dict[str, Any]:
    """
    Update the working notes when the agent/user "replies".
    tools.py passes a pydantic body with: { session_id, latest_user_text, locale }
    Default behavior: append the text into the notes buffer.
    """
    if payload and isinstance(payload, dict):
        session_id = payload.get("session_id", session_id)
        latest_user_text = payload.get("latest_user_text", latest_user_text)
        locale = payload.get("locale", locale)
        mode = payload.get("mode", mode)  # optional: "append" | "overwrite"

    if not session_id:
        return {"ok": False, "message": "session_id required"}

    mode = (mode or "append").lower()
    if mode == "overwrite":
        return set_notes(session_id, latest_user_text or "")

    # default: append
    return append_notes(session_id, latest_user_text or "")


async def handle_summary_objective(
    payload: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    locale: Optional[str] = None,
    **kwargs,
) -> Dict[str, Any]:
    """
    Return a short 'objective' style snapshot derived from current notes (MVP).
    Downstream can replace this with a real objective extraction pipeline.
    """
    if payload and isinstance(payload, dict):
        session_id = payload.get("session_id", session_id)
        locale = payload.get("locale", locale)

    if not session_id:
        return {"ok": False, "message": "session_id required"}

    notes = get_notes(session_id)
    snippet = (notes[:300] + "…") if len(notes) > 300 else (notes or "")
    objective = {
        "vitals": "N/A (voice intake MVP)",
        "observations": snippet,
        "locale": locale or "en",
    }
    return {
        "ok": True,
        "session_id": session_id,
        "objective": objective,
        "message": "Objective snapshot generated (stub).",
    }


async def handle_summary_finalize(
    payload: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    preview_only: Optional[bool] = True,
    locale: Optional[str] = None,
    **kwargs,
) -> Dict[str, Any]:
    """
    Finalize or preview a SOAP draft from notes.
    If preview_only=True (default), return a draft; otherwise you'd persist.
    """
    if payload and isinstance(payload, dict):
        session_id = payload.get("session_id", session_id)
        preview_only = payload.get("preview_only", preview_only)
        locale = payload.get("locale", locale)

    if not session_id:
        return {"ok": False, "message": "session_id required"}

    draft_result = finalize_soap(session_id)
    # In a real pipeline, if preview_only is False, you'd also save it here.
    draft_result.update({
        "preview_only": bool(preview_only),
        "locale": locale or "en",
    })
    return draft_result
