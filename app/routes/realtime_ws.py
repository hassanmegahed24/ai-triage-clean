# app/routes/realtime_ws.py
import os
import json
import uuid
import re
from typing import Dict, Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, Body
from fastapi.responses import JSONResponse
import websockets

# ---------- Config ----------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
REALTIME_MODEL = os.getenv("REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
REALTIME_VOICE = os.getenv("REALTIME_VOICE", "verse")

# Prompt load (session instructions)
from pathlib import Path
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "realtime_voice_intake.md"
try:
    PROMPT_TEXT = PROMPT_PATH.read_text(encoding="utf-8")
except Exception:
    PROMPT_TEXT = (
        "You are a concise, clinically-safe triage assistant. "
        "Speak briefly, one question at a time, use tools for Observation/SOAP, and do not read long documents aloud."
    )

# --- one-time debug peek so we can confirm file + content quickly ---
print(
    "[PROMPT] loaded",
    f"path={PROMPT_PATH}",
    f"len={len(PROMPT_TEXT)}",
    "head=" + PROMPT_TEXT[:120].replace("\n", " ") + "..."
)

router = APIRouter()

# ---------- In-memory stores (MVP) ----------
# Tools are the only writers to these now.
LIVE_OBSERVATION: Dict[str, str] = {}         # session_id -> observation text (save_observation)
LIVE_SOAP: Dict[str, Dict[str, Any]] = {}     # session_id -> SOAP dict (finalize_soap)
TOOL_ARG_BUFFERS: Dict[str, str] = {}         # tool_call_id -> streamed JSON args
SESSION_FLAGS: Dict[str, Dict[str, Any]] = {} # per-session state (e.g., finalized_once)

MAX_NOTES_LEN = 12000  # kept for compatibility

def _normalize_live(text: str) -> str:
    if text is None:
        return ""
    paragraphs = re.split(r"\n\s*\n", text.strip())
    cleaned = []
    for p in paragraphs:
        cleaned.append(re.sub(r"\s+", " ", p.strip()))
    return "\n\n".join([c for c in cleaned if c])

def _content_to_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if text:
            parts.append(str(text))
    return "\n".join(parts).strip()


def _coerce_patient_id(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ingest_conversation_item(session_id: str, item: Dict[str, Any]) -> None:
    if not item:
        return
    role = item.get("role")
    text = _content_to_text(item.get("content"))
    if not text or role not in {"user", "assistant"}:
        return
    try:
        if role == "user":
            add_doctor_message(session_id, text, modality="voice")
        elif role == "assistant":
            add_assistant_reply(session_id, text, modality="voice")
    except KeyError:
        # Summary session not yet initialized; ignore.
        pass


async def _send_snapshot_to_model(oa_ws, session_id: str, snapshot: Dict[str, Any], patient_id: Optional[str]) -> None:
    if not snapshot:
        return
    try:
        snapshot_text = json.dumps(snapshot, indent=2)
    except Exception:
        snapshot_text = str(snapshot)
    header = f"Patient Snapshot (session {session_id})"
    if patient_id:
        header = f"Patient Snapshot (patient {patient_id})"
    message = f"{header}:\n{snapshot_text}"
    await oa_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "system",
            "content": [
                {"type": "input_text", "text": message}
            ]
        }
    }))


async def _initialize_summary_context(session_id: str, ctx: Dict[str, Any], oa_ws, browser_ws) -> None:
    flags = SESSION_FLAGS.setdefault(session_id, {
        "finalized_once": False,
        "recent_save": False,
        "summary_ready": False,
        "snapshot_sent": False,
    })

    raw_patient_id = ctx.get("patient_id")
    patient_id = _coerce_patient_id(raw_patient_id)
    doctor_id = ctx.get("doctor_id")
    locale = ctx.get("locale") or "en"
    consent = bool(ctx.get("consent", True))
    snapshot = ctx.get("snapshot")
    preload_notes = ctx.get("notes")

    # Fetch snapshot if not provided and patient id exists
    if snapshot is None and patient_id is not None:
        try:
            snapshot = await build_snapshot(patient_id)
        except Exception as e:
            await browser_ws.send_text(json.dumps({
                "type": "session.context.error",
                "session_id": session_id,
                "message": f"Snapshot load failed: {e}"
            }))
            snapshot = {}

    try:
        summary = get_summary_session(session_id)
        if patient_id is not None:
            summary.patient_id = patient_id
        if doctor_id is not None:
            summary.doctor_id = doctor_id
        if locale:
            summary.locale = locale
        if snapshot:
            summary.snapshot = snapshot
    except KeyError:
        # If patient_id missing, fall back to placeholder 0
        summary = create_session(
            session_id=session_id,
            patient_id=patient_id or 0,
            doctor_id=doctor_id,
            consent=consent,
            locale=locale,
            snapshot=snapshot or {},
        )

    if preload_notes:
        tool_handlers.set_notes(session_id, preload_notes)
        normalized = tool_handlers.get_notes(session_id)
        set_working_notes(session_id, normalized)
        LIVE_OBSERVATION[session_id] = _normalize_live(normalized)

    flags.update({
        "patient_id": summary.patient_id,
        "doctor_id": summary.doctor_id,
        "locale": summary.locale,
        "snapshot_loaded": bool(summary.snapshot),
        "summary_ready": True,
        "recent_save": bool(preload_notes),
    })

    if summary.snapshot and not flags.get("snapshot_sent"):
        await _send_snapshot_to_model(oa_ws, session_id, summary.snapshot, summary.patient_id)
        flags["snapshot_sent"] = True

    await browser_ws.send_text(json.dumps({
        "type": "session.context.ready",
        "session_id": session_id,
        "summary_session_id": summary.session_id,
        "snapshot_loaded": bool(summary.snapshot),
        "patient_id": raw_patient_id or summary.patient_id,
    }))
# Tool handlers
from app.services import realtime_tool_handlers as tool_handlers
from app.services.summary_session import (
    create_session,
    get_session as get_summary_session,
    set_working_notes,
    add_doctor_message,
    add_assistant_reply,
)
from app.services.snapshot_builder import build_snapshot


async def _handle_tool_call_event(ev: Dict[str, Any], oa_ws, browser_ws, session_id: str) -> bool:
    """
    Intercept OpenAI tool call events, run Python handler, and send tool.output back upstream.
    Also send UI preview events to the browser (for SOAP), and set per-session flags to prevent loops.
    Return True to swallow (don't forward to browser), False otherwise.
    """
    ev_type = ev.get("type", "")

    # Arguments streaming (two common shapes)
    if (
        ev_type.endswith("arguments.delta")
        or ev_type.endswith("tool_call.arguments.delta")
        or ev_type.endswith("output_item.delta")
    ):
        call_id = ev.get("call_id") or ev.get("tool_call_id")
        delta = ev.get("delta") or ""
        name = ev.get("name")
        if not call_id:
            item = ev.get("item") or {}
            call_id = item.get("call_id") or item.get("id")
            name = item.get("name") or name
            delta = item.get("delta") or delta
        if call_id and delta:
            TOOL_ARG_BUFFERS[call_id] = TOOL_ARG_BUFFERS.get(call_id, "") + delta
        print(f"[TOOLS] args.delta name={name} call_id={call_id} len+={len(delta)}")
        return True

    # Arguments done → dispatch tool
    if (
        ev_type.endswith("arguments.done")
        or ev_type.endswith("tool_call.arguments.done")
        or ev_type.endswith("output_item.done")
    ):
        call_id = ev.get("call_id") or ev.get("tool_call_id")
        name = ev.get("name")
        args_json = None
        if not call_id:
            item = ev.get("item") or {}
            call_id = item.get("call_id") or item.get("id")
            name = item.get("name") or name
            args_json = item.get("arguments")
        if args_json is None:
            args_json = TOOL_ARG_BUFFERS.pop(call_id, "{}")
        else:
            TOOL_ARG_BUFFERS.pop(call_id, None)
        try:
            args = json.loads(args_json) if args_json else {}
        except Exception:
            args = {}
        print(f"[TOOLS] args.done  name={name} call_id={call_id} args={args}")

        # --- auto-inject session_id so the model can omit it in tool calls ---
        args.setdefault("session_id", session_id)
        sid_arg = args.get("session_id") or session_id
        if isinstance(sid_arg, str) and sid_arg.strip().lower() in {"<sid>", "session_id", "<session_id>"}:
            sid_arg = session_id
            args["session_id"] = sid_arg

        

        result = {"ok": False, "message": f"Unknown tool: {name}"}
        tiny_ack = None

        ui_event_payload: Optional[Dict[str, Any]] = None

        if name == "save_observation":
            # Expect handler returns { ok, observation, message? } or { ok, notes, ... }
            result = await tool_handlers.save_observation(
                session_id=sid_arg,
                notes=args.get("notes", "")
            )
            obs = result.get("observation") or result.get("notes") or ""
            LIVE_OBSERVATION[sid_arg] = _normalize_live(obs)
            SESSION_FLAGS.setdefault(sid_arg, {}).update({"recent_save": True})
            tiny_ack = "saved"
            print(f"[TOOLS] save_observation stored len={len(LIVE_OBSERVATION[sid_arg])}")

            ui_event_payload = {
                "type": "ui.observation.preview",
                "session_id": sid_arg,
                "notes": LIVE_OBSERVATION[sid_arg],
                "observation": LIVE_OBSERVATION[sid_arg],
                "message": result.get("message", ""),
            }

        elif name == "finalize_soap":
            sid = sid_arg
            flags = SESSION_FLAGS.setdefault(sid, {"finalized_once": False, "recent_save": False})
            notes_arg = args.get("notes")
            if notes_arg:
                try:
                    tool_handlers.set_notes(sid, notes_arg)
                except Exception as e:
                    print(f"[TOOLS] finalize_soap notes set failed: {e}")
            result = await tool_handlers.finalize_soap(session_id=sid)
            soap = result.get("soap") or {}
            LIVE_SOAP[session_id] = soap
            flags["finalized_once"] = True
            flags["recent_save"] = False
            print(f"[TOOLS] finalize_soap stored keys={list(soap.keys()) or 'empty'}")

            # Let the doctor know the draft is ready; model stays available for follow-up.
            try:
                await oa_ws.send(json.dumps({
                    "type": "response.create",
                    "response": {
                        "modalities": ["audio", "text"],
                        "instructions": (
                            "Doctor, the SOAP draft is ready. "
                            "Let me know if you need revisions or another pass."
                        )
                    }
                }))
            except Exception:
                pass

            ui_event_payload = {
                "type": "ui.soap.preview",
                "session_id": session_id,
                "soap": soap,
                "message": result.get("message", ""),
            }

            if not LIVE_OBSERVATION.get(session_id):
                try:
                    fallback_notes = tool_handlers.get_notes(sid)
                except Exception:
                    fallback_notes = ""
                if fallback_notes:
                    LIVE_OBSERVATION[session_id] = _normalize_live(fallback_notes)
                    print(f"[TOOLS] finalize_soap backfilled live notes len={len(LIVE_OBSERVATION[session_id])}")

        # Send tool output upstream using conversation.item.create
        tool_output_msg = {
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result),
            },
        }
        print(f"[TOOLS] output     name={name} call_id={call_id} -> {result}")
        await oa_ws.send(json.dumps(tool_output_msg))

        if ui_event_payload:
            try:
                await browser_ws.send_text(json.dumps(ui_event_payload))
            except Exception as ui_err:
                print(f"[TOOLS] ui event send failed: {ui_err}")

        # Tiny audio ack only for save_observation (SOAP already got an explicit pause message)
        if tiny_ack:
            try:
                await oa_ws.send(json.dumps({
                    "type": "response.create",
                    "response": {
                        "modalities": ["audio"],
                        "instructions": tiny_ack
                    }
                }))
            except Exception as e:
                print(f"[TOOLS] ack send failed: {e}")

        return True

    # Not a tool event we handle
    return False


# ---------- Live Notes REST Endpoints ----------
@router.get("/realtime/live-notes")
async def get_live_notes(session_id: str = Query(..., description="Session ID from session.id event")):
    obs = LIVE_OBSERVATION.get(session_id, "")
    if not obs:
        try:
            obs = _normalize_live(tool_handlers.get_notes(session_id))
        except Exception:
            obs = ""
    return {
        "session_id": session_id,
        "observation": obs,
        "notes": obs,  # alias for legacy UI bindings
        "soap": LIVE_SOAP.get(session_id, None),
    }


@router.put("/realtime/live-notes")
async def put_live_notes(payload: Dict[str, Any] = Body(...)):
    """
    Optional: keep this to allow doctor-edited notes to be saved explicitly.
    It's NOT fed by transcript deltas anymore—only manual edits or future tooling.
    """
    session_id = payload.get("session_id", "")
    notes = payload.get("notes", "")
    if not session_id:
        return JSONResponse({"ok": False, "message": "session_id required"}, status_code=400)
    LIVE_OBSERVATION[session_id] = _normalize_live(notes or "")
    return {"ok": True, "session_id": session_id, "len": len(LIVE_OBSERVATION[session_id])}


# ---------- Prompt debug endpoint ----------
@router.get("/realtime/prompt")
async def get_realtime_prompt():
    return {
        "model": REALTIME_MODEL,
        "voice": REALTIME_VOICE,
        "length": len(PROMPT_TEXT),
        "head": PROMPT_TEXT[:3000],
    }


# ---------- Browser <-> WS Bridge ----------
@router.websocket("/realtime/ws")
async def realtime_ws(websocket: WebSocket):
    await websocket.accept()

    # Per-connection session id (browser uses this to poll live notes)
    session_id = str(uuid.uuid4())
    SESSION_FLAGS[session_id] = {
        "finalized_once": False,
        "recent_save": False,
        "summary_ready": False,
        "snapshot_sent": False,
    }
    LIVE_OBSERVATION.setdefault(session_id, "")
    LIVE_SOAP.setdefault(session_id, None)
    print(f"[WS] new session_id={session_id}")
    await websocket.send_text(json.dumps({"type": "session.id", "session_id": session_id}))

    # Connect upstream to OpenAI Realtime WS
    url = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"
    try:
        oa_ws = await websockets.connect(
            url,
            extra_headers=[
                ("Authorization", f"Bearer {OPENAI_API_KEY}"),
                ("OpenAI-Beta", "realtime=v1"),
            ],
            max_size=20_000_000,
            ping_interval=20,
            ping_timeout=20,
        )
    except Exception as e:
        await websocket.send_text(json.dumps({"type": "error", "message": f"Upstream connect failed: {e}"}))
        await websocket.close()
        return

    # Send initial session.update (modalities, voice, formats, VAD, tools, instructions)
    initial_session = {
        "type": "session.update",
        "session": {
            "modalities": ["text", "audio"],
            "voice": REALTIME_VOICE,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {"model": "whisper-1"},
            # Stronger VAD to prevent re-entrant turns while speaking
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.90,
                "silence_duration_ms": 1500,
                "prefix_padding_ms": 170
            },
            "temperature": 0.6,
            "tool_choice": "auto",
            "tools": [
                {
                    "type": "function",
                    "name": "save_observation",
                    "description": "Persist the current observation/notes for this session to storage.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string"},
                            "notes": {"type": "string"}
                        },
                        "required": ["session_id", "notes"]
                    }
                },
                {
                    "type": "function",
                    "name": "finalize_soap",
                    "description": "Synthesize a SOAP draft from the current working notes for physician review.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string"},
                            "notes": {
                                "type": "string",
                                "description": "Optional recap of the latest findings to feed into the SOAP draft."
                            }
                        },
                        "required": ["session_id"]
                    }
                }
            ],
            "instructions": PROMPT_TEXT,
        },
    }

    # Defensive: send session.update twice to avoid first-turn race
    await oa_ws.send(json.dumps(initial_session))
    await oa_ws.send(json.dumps(initial_session))

    # STRONG PIN: Inject the prompt as a system message in the conversation state
    system_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "system",
            "content": [
                {"type": "input_text", "text": PROMPT_TEXT}
            ]
        }
    }
    await oa_ws.send(json.dumps(system_item))

    # Optional: brief role acknowledgement
    await oa_ws.send(json.dumps({
        "type": "response.create",
        "response": {
            "modalities": ["text", "audio"],
            "instructions": "ROLE-ACK: supervised intake agent."
        }
    }))

    # ------------ Bridge coroutines ------------
    async def from_browser_to_openai():
        try:
            while True:
                data = await websocket.receive_text()
                try:
                    msg = json.loads(data)
                except Exception:
                    continue

                mtype = msg.get("type")

                # audio from browser -> input buffer append
                if mtype == "audio.append":
                    await oa_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": msg.get("audio", "")
                    }))
                    continue

                if mtype == "audio.commit":
                    await oa_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    continue

                # browser convenience: response.create passthrough
                if mtype == "response.create":
                    await oa_ws.send(json.dumps(msg))
                    continue

                # optional: simple text convenience -> response.create
                if mtype == "text":
                    text = msg.get("text", "")
                    await oa_ws.send(json.dumps({
                        "type": "response.create",
                        "response": {
                            "modalities": ["audio", "text"],
                            "instructions": text
                        }
                    }))
                    continue

                if mtype == "session.context":
                    await _initialize_summary_context(session_id, msg, oa_ws, websocket)
                    continue

                # fallback passthrough
                await oa_ws.send(json.dumps(msg))
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    async def from_openai_to_browser():
        try:
            async for raw in oa_ws:
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue

                # IMPORTANT: Do NOT append transcript/text deltas to live notes anymore.
                # Only tools (save_observation/finalize_soap) write to LIVE_OBSERVATION/LIVE_SOAP.

                # Intercept tool calls; if handled, don't forward to browser
                try:
                    swallow = await _handle_tool_call_event(ev, oa_ws, websocket, session_id)
                    if swallow:
                        continue
                except Exception:
                    # fail-soft; keep relay going
                    pass

                try:
                    if ev.get("type") == "conversation.item.create":
                        _ingest_conversation_item(session_id, ev.get("item"))
                except Exception:
                    pass

                try:
                    flags = SESSION_FLAGS.setdefault(session_id, {
                        "finalized_once": False,
                        "recent_save": False,
                        "summary_ready": False,
                        "snapshot_sent": False,
                    })
                    if ev.get("type") in ("response.audio_transcript.delta", "response.audio_transcript.done"):
                        transcript = (ev.get("delta") or ev.get("transcript") or "").lower()
                        if transcript:
                            if "save_observation" in transcript and not flags.get("recent_save"):
                                msg = (
                                    "I have not called save_observation yet. I must call the save_observation tool now "
                                    "so your notes appear."
                                )
                                print(f"[TOOLS] transcript claim without save_observation; prompting save (session={session_id})")
                                try:
                                    await oa_ws.send(json.dumps({
                                        "type": "response.create",
                                        "response": {
                                            "modalities": ["audio", "text"],
                                            "instructions": msg
                                        }
                                    }))
                                except Exception:
                                    pass
                            if "finalize_soap" in transcript and not flags.get("finalized_once"):
                                reminder = (
                                    "I must call save_observation with the latest notes, then call finalize_soap. "
                                    "Running those tools now."
                                )
                                print(f"[TOOLS] transcript claim without finalize_soap; prompting tool call (session={session_id})")
                                try:
                                    await oa_ws.send(json.dumps({
                                        "type": "response.create",
                                        "response": {
                                            "modalities": ["audio", "text"],
                                            "instructions": reminder
                                        }
                                    }))
                                except Exception:
                                    pass
                except Exception:
                    pass

                # Relay everything else to browser
                await websocket.send_text(json.dumps(ev))
        except Exception:
            # upstream closed or error
            try:
                await websocket.close()
            except Exception:
                pass

    import asyncio
    consumer = asyncio.create_task(from_browser_to_openai())
    producer = asyncio.create_task(from_openai_to_browser())

    done, pending = await asyncio.wait(
        [consumer, producer],
        return_when=asyncio.FIRST_COMPLETED
    )

    for task in pending:
        task.cancel()
    try:
        await oa_ws.close()
    except Exception:
        pass
