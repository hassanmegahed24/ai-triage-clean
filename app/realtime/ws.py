from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from typing import Optional, List, Dict, Any
import time
import os
import asyncio
import base64
from types import SimpleNamespace
from pathlib import Path

from app.clients.realtime_client import RealtimeClient
from app.clients.reasoning_client import ReasoningClient
from app.realtime.context import make_session_instructions
from app.services.snapshot_builder import build_snapshot


router = APIRouter()


@router.get("/test")
def realtime_test_page():
    try:
        html_path = Path(__file__).resolve().parent / "test.html"
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    except Exception as e:
        return HTMLResponse(f"<pre>Failed to load test.html: {e}</pre>", status_code=500)


@router.websocket("/ws")
async def realtime_ws(websocket: WebSocket):
    await websocket.accept()

    client: Optional[RealtimeClient] = None
    reason_client: Optional[ReasoningClient] = None

    send_lock = asyncio.Lock()
    turns: List[Dict[str, Any]] = []
    assistant_buf: List[str] = []
    mute_audio: bool = False
    session_finalized: bool = False
    awaiting_doctor_approval: bool = False
    final_ack_pending: bool = False
    cancel_audio_swallow_until_ts: float = 0.0
    # half-duplex / VAD gating
    user_speaking: bool = False
    user_audio_bytes: int = 0
    assistant_speaking: bool = False
    last_response_finished_ts: float = 0.0
    next_create_earliest_ts: float = 0.0
    MIN_USER_BYTES: int = 6000        # require ~>100ms of 24k PCM16 to avoid noise
    CREATE_COOLDOWN_S: float = 1.2    # avoid rapid re-create loops

    # default patient id from env (used by voice-confirm finalize)
    _env_pid = (os.getenv("REALTIME_PATIENT_ID", "").strip())
    default_patient_id: Optional[int] = int(_env_pid) if _env_pid.isdigit() else None

    async def ws_send_json(payload: dict) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def do_finalize(patient_id: int, extra_context: Optional[str] = None) -> None:
        nonlocal mute_audio, session_finalized, client
        if session_finalized:
            return
        # cancel any in-flight realtime response
        try:
            if client and client.is_connected and (client.active_resp_id or client.pending_create):
                await client.cancel_response()
        except Exception:
            pass
        # build conversation context
        conv = list(turns[-50:])
        if extra_context:
            conv.append({"role": "doctor", "content": extra_context})
        try:
            snap = await build_snapshot(int(patient_id))
            ns_turns = [SimpleNamespace(**t) for t in conv]
            assert reason_client is not None
            js = await reason_client.generate_summary_finalize(
                turns=ns_turns,
                snapshot=snap,
                locale=(os.getenv("REALTIME_DEFAULT_LANGUAGE") or "en"),
                preview_only=False,
            )
            await ws_send_json({
                "type": "soap.result",
                "preview": False,
                "soap": js.get("soap", {}),
                "speech_output": js.get("speech_output"),
                "confidence": js.get("confidence"),
                "suggested_actions": js.get("suggested_actions"),
            })
            session_finalized = True
            try:
                if client:
                    await client.disconnect()
            except Exception:
                pass
        except Exception as e:
            await ws_send_json({"type": "error", "message": f"finalize failed: {e}"})

    try:
        client = RealtimeClient()
        reason_client = ReasoningClient()

        # optionally set session instructions with snapshot
        try:
            sys_file = os.getenv("REALTIME_SYSTEM_FILE", "system_global.txt")
            pid_env = os.getenv("REALTIME_PATIENT_ID", "").strip()
            if pid_env.isdigit():
                try:
                    instr = await make_session_instructions(int(pid_env), sys_file)
                    client.session_config["session"]["instructions"] = instr
                except Exception:
                    pass
        except Exception:
            pass

        async def on_audio_response(audio_bytes: bytes) -> None:
            nonlocal mute_audio, session_finalized, cancel_audio_swallow_until_ts
            if mute_audio or session_finalized:
                return
            if time.monotonic() < cancel_audio_swallow_until_ts:
                return
            await ws_send_json({
                "type": "response.audio.delta",
                "audio": base64.b64encode(audio_bytes).decode("utf-8"),
            })

        async def on_text_response(text: str) -> None:
            nonlocal awaiting_doctor_approval, mute_audio
            assistant_buf.append(text)
            # If muted, don't forward more text deltas to client UI to avoid chatter
            if mute_audio:
                return
            await ws_send_json({
                "type": "response.text.delta",
                "delta": text,
            })
            # detect confirmation prompt
            try:
                t = text.lower()
                prompts = [
                    "\u8bf7\u786e\u8ba4",           # 璇风‘璁?                    "\u662f\u5426\u786e\u8ba4",     # 鏄惁纭
                    "\u786e\u8ba4\u5417",           # 纭鍚?                    "\u662f\u5426\u540c\u610f",     # 鏄惁鍚屾剰
                    "\u533b\u751f",                   # 鍖荤敓
                    "approve", "approval", "confirm", "confirmation",
                ]
                if any(k in t for k in prompts):
                    awaiting_doctor_approval = True
            except Exception:
                pass

        async def on_error(msg: str) -> None:
            await ws_send_json({"type": "error", "message": msg})

        client.on_audio_response = on_audio_response
        client.on_text_response = on_text_response
        client.on_error = on_error

        async def _on_resp_started() -> None:
            nonlocal assistant_speaking
            assistant_buf.clear()
            assistant_speaking = True
        async def _on_resp_finished() -> None:
            nonlocal assistant_speaking, last_response_finished_ts, final_ack_pending, mute_audio
            if assistant_buf:
                turns.append({"role": "assistant", "content": "".join(assistant_buf)})
                assistant_buf.clear()
            assistant_speaking = False
            last_response_finished_ts = time.monotonic()
            # If we just played the final ack line, immediately mute further audio
            if final_ack_pending:
                final_ack_pending = False
                mute_audio = True
                try:
                    await ws_send_json({"type": "tts.mute"})
                except Exception:
                    pass
        client.on_response_started = _on_resp_started
        client.on_response_finished = _on_resp_finished

        async def _on_speech_started() -> None:
            nonlocal user_speaking, user_audio_bytes
            user_speaking = True
            user_audio_bytes = 0

        async def _on_speech_stopped() -> None:
            # Only create a response when: not speaking, enough user audio, cooldown passed
            nonlocal user_speaking, user_audio_bytes, next_create_earliest_ts, mute_audio
            user_speaking = False
            if session_finalized:
                return
            if mute_audio:
                return
            now = time.monotonic()
            if assistant_speaking:
                return
            if user_audio_bytes < MIN_USER_BYTES:
                return
            if now < next_create_earliest_ts:
                return
            # gate shortly after assistant finished to avoid overlap
            if (now - last_response_finished_ts) < 0.25:
                return
            try:
                await client.create_response()  # type: ignore
                next_create_earliest_ts = now + CREATE_COOLDOWN_S
            except Exception:
                pass

        # hook VAD callbacks
        try:
            client.on_speech_started = _on_speech_started  # type: ignore[attr-defined]
            client.on_speech_stopped = _on_speech_stopped  # type: ignore[attr-defined]
        except Exception:
            pass
        async def _on_user_transcript(text: str) -> None:
            nonlocal awaiting_doctor_approval, mute_audio, session_finalized, final_ack_pending
            # record patient speech as a turn
            turns.append({"role": "patient", "content": text})
            # if the model asked to confirm, detect doctor's confirm words
            try:
                if awaiting_doctor_approval and not session_finalized:
                    tl = (text or "").strip().lower()
                    confirm_words = (
                        "\u6211\u540c\u610f",  # 鎴戝悓鎰?                        "\u786e\u8ba4",        # 纭
                        "\u540c\u610f",        # 鍚屾剰
                        "\u53ef\u4ee5",        # 鍙互
                        "\u786e\u5b9a",        # 纭畾
                        "\u6279\u51c6",        # 鎵瑰噯
                        "\u751f\u6210\u5427",  # 鐢熸垚鍚?                        "\u6ca1\u95ee\u9898",  # 娌￠棶棰?                        "\u597d\u7684",        # 濂界殑
                        "\u597d",              # 濂?                        "\u884c",              # 琛?                        "\u662f\u7684",        # 鏄殑
                        "\u53ef\u4ee5\u751f\u6210",  # 鍙互鐢熸垚
                        "\u5f00\u59cb\u751f\u6210",  # 寮€濮嬬敓鎴?                        "ok", "okay", "yes", "yep", "approve", "approved", "confirmed", "confirm",
                    )
                    if any(w in tl for w in confirm_words):
                        if default_patient_id is not None:
                            # Disable auto create to avoid loops
                            try:
                                if client:
                                    client.auto_create_on_silence = False  # type: ignore[attr-defined]
                            except Exception:
                                pass

                            # Cancel any in-flight response and speak exactly one short ack line
                            try:
                                if client and client.is_connected and (client.active_resp_id or client.pending_create):
                                    await client.cancel_response()
                            except Exception:
                                pass

                            # Allow this single ack to play, then we'll mute in _on_resp_finished
                            mute_audio = False
                            final_ack_pending = True
                            try:
                                if client:
                                    await client.create_response_with_instructions("好的医生，笔记已经准备好，可以点击按钮来查看")
                            except Exception:
                                pass

                            # Kick off finalize in background so the ack can play immediately
                            try:
                                asyncio.create_task(do_finalize(default_patient_id))
                            except Exception:
                                # If scheduling task fails, fall back to direct await
                                await do_finalize(default_patient_id)
                            awaiting_doctor_approval = False
            except Exception:
                pass

        client.on_user_transcript = _on_user_transcript

        await client.connect()

        while True:
            message = await websocket.receive_json()
            mtype = message.get("type")

            if mtype == "audio.append":
                data_b64 = message.get("audio") or ""
                if data_b64 and not session_finalized:
                    try:
                        audio_bytes = base64.b64decode(data_b64)
                        if user_speaking:
                            user_audio_bytes += len(audio_bytes)
                        await client.send_audio(audio_bytes)
                    except Exception as e:
                        await ws_send_json({"type": "error", "message": f"audio.append failed: {e}"})

            elif mtype == "audio.commit":
                if not session_finalized:
                    try:
                        await client.commit_audio()
                    except Exception as e:
                        await ws_send_json({"type": "error", "message": f"audio.commit failed: {e}"})

            elif mtype == "response.create":
                if not session_finalized:
                    try:
                        await client.create_response()
                    except Exception as e:
                        await ws_send_json({"type": "error", "message": f"response.create failed: {e}"})

            elif mtype == "response.create_with_instructions":
                if not session_finalized:
                    text = message.get("text") or ""
                    try:
                        await client.create_response_with_instructions(text)
                    except Exception as e:
                        await ws_send_json({"type": "error", "message": f"response.create_with_instructions failed: {e}"})

            elif mtype == "response.cancel":
                cancel_audio_swallow_until_ts = time.monotonic() + 0.6
                try:
                    if client and client.is_connected and (client.active_resp_id or client.pending_create):
                        await client.cancel_response()
                except Exception:
                    pass

            elif mtype == "control.stop":
                # hard stop: cancel current response, mute future audio, ack client
                try:
                    mute_audio = True
                    if client:
                        client.auto_create_on_silence = False  # type: ignore[attr-defined]
                        if client.is_connected and (client.active_resp_id or client.pending_create):
                            await client.cancel_response()
                except Exception:
                    pass
                await ws_send_json({"type": "control.stop.ack"})

            elif mtype == "session.update":
                try:
                    await client.send_session_update()
                except Exception as e:
                    await ws_send_json({"type": "error", "message": f"session.update failed: {e}"})

            elif mtype == "finalize.force":
                try:
                    pid_val = message.get("patient_id")
                    if pid_val is None:
                        env_pid = (os.getenv("REALTIME_PATIENT_ID", "").strip())
                        pid_val = int(env_pid) if env_pid.isdigit() else None
                    if pid_val is None:
                        raise ValueError("patient_id required (message.patient_id or REALTIME_PATIENT_ID)")
                    extra_ctx = (message.get("context") or "").strip() or None
                    await ws_send_json({"type": "finalize.ack", "patient_id": pid_val})
                    await do_finalize(int(pid_val), extra_ctx)
                except Exception as e:
                    await ws_send_json({"type": "error", "message": f"finalize.force failed: {e}"})

            elif mtype in ("soap.preview", "soap.finalize"):
                try:
                    pid_val = message.get("patient_id")
                    if pid_val is None:
                        env_pid = (os.getenv("REALTIME_PATIENT_ID", "").strip())
                        pid_val = int(env_pid) if env_pid.isdigit() else None
                    if pid_val is None:
                        raise ValueError("patient_id required (message.patient_id or REALTIME_PATIENT_ID)")

                    locale = (message.get("locale") or os.getenv("REALTIME_DEFAULT_LANGUAGE") or "en").strip()
                    context_text = (message.get("context") or "").strip()
                    conv = list(turns[-50:])
                    context_text = (message.get("context") or "").strip()
                    if context_text:
                        conv.append({"role": "doctor", "content": context_text})
                    if context_text:
                        conv.append({"role": "doctor", "content": context_text})

                    snap = await build_snapshot(int(pid_val))
                    preview_only = (mtype == "soap.preview")
                    # do not send tts.mute; keep audio pipeline as-is
                    ns_turns = [SimpleNamespace(**t) for t in conv]
                    assert reason_client is not None
                    js = await reason_client.generate_summary_finalize(
                        turns=ns_turns,
                        snapshot=snap,
                        locale=locale or "en",
                        preview_only=preview_only,
                    )
                    await ws_send_json({
                        "type": "soap.result",
                        "preview": preview_only,
                        "soap": js.get("soap", {}),
                        "speech_output": js.get("speech_output"),
                        "confidence": js.get("confidence"),
                        "suggested_actions": js.get("suggested_actions"),
                    })
                    if preview_only:
                        await ws_send_json({
                            "type": "ui.soap.preview",
                            "soap": js.get("soap", {}),
                            "speech_output": js.get("speech_output"),
                            "confidence": js.get("confidence"),
                            "suggested_actions": js.get("suggested_actions"),
                        })
                    if not preview_only:
                        session_finalized = True
                        try:
                            if client:
                                await client.disconnect()
                        except Exception:
                            pass
                except Exception as e:
                    await ws_send_json({"type": "error", "message": f"soap generation failed: {e}"})

            elif mtype == "objective.preview":
                try:
                    pid_val = message.get("patient_id")
                    if pid_val is None:
                        env_pid = (os.getenv("REALTIME_PATIENT_ID", "").strip())
                        pid_val = int(env_pid) if env_pid.isdigit() else None
                    if pid_val is None:
                        raise ValueError("patient_id required (message.patient_id or REALTIME_PATIENT_ID)")

                    locale = (message.get("locale") or os.getenv("REALTIME_DEFAULT_LANGUAGE") or "en").strip()
                    conv = list(turns[-50:])

                    snap = await build_snapshot(int(pid_val))
                    ns_turns = [SimpleNamespace(**t) for t in conv]
                    assert reason_client is not None
                    js_obj = await reason_client.generate_objective_only(
                        turns=ns_turns,
                        snapshot=snap,
                        locale=locale or "en",
                    )
                    await ws_send_json({
                        "type": "objective.result",
                        "objective": js_obj.get("objective"),
                        "speech_output": js_obj.get("speech_output"),
                        "confidence": js_obj.get("confidence"),
                        "suggested_actions": js_obj.get("suggested_actions"),
                    })
                    await ws_send_json({
                        "type": "ui.objective.preview",
                        "objective": js_obj.get("objective"),
                        "speech_output": js_obj.get("speech_output"),
                        "confidence": js_obj.get("confidence"),
                        "suggested_actions": js_obj.get("suggested_actions"),
                    })
                except Exception as e:
                    await ws_send_json({"type": "error", "message": f"objective.preview failed: {e}"})

            else:
                await ws_send_json({"type": "warn", "message": f"unknown message type: {mtype}"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws_send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass



