# app/routes/summary.py
# ------------------------------------------------------------
# Summary Agent routes — MVP slice:
#   This file will eventually contain:
#     /summary/start, /summary/message, /summary/upload, /summary/reply, /summary/finalize
#   For now we only implement /summary/start (create a session + cache snapshot).
# ------------------------------------------------------------

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from typing import Optional, List, Literal, Dict, Any
from app.models.summary import SummaryStartRequest, SummaryStartResponse, SummaryMessageRequest, SummaryMessageResponse,ResponseEnvelope, SummaryFinalizeRequest, ObjectiveRequest #add envelope
from app.services.snapshot_builder import build_snapshot
from app.services.summary_session import create_session, get_session, add_doctor_message, add_assistant_reply, mark_finalized, mark_saved
from app.utils.time import now_et
from app.clients.reasoning_client import ReasoningClient
from app.services.visit_writer import save_visit_if_ready
from pydantic import BaseModel, Field


router = APIRouter()
reason_client = ReasoningClient()


@router.post("/start", response_model=SummaryStartResponse)
async def start_summary(body: SummaryStartRequest) -> SummaryStartResponse:
    '''Flow:
    1) Validate input (pydantic model already validated types/required fields).
    2) Fetch snapshot once via build_snapshot(patient_id).
    3) Create an in-memory session with the snapshot, consent, locale, doctor_id.
    4) Return a SummaryStartResponse with session_id and 'collecting' status. '''
    # Step 1: Validate input (pydantic already validated types/required fields).
    patient_id = body.patient_id
    doctor_id = body.doctor_id
    locale = body.locale or "en"
    consent = bool(body.consent)

    # Step 2: Fetch snapshot once via build_snapshot(patient_id).
    try:
        snapshot = await build_snapshot(patient_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error building snapshot: {e}")
    
    # Step 3: Create an in-memory session with the snapshot, consent, locale, doctor_id.
    sess = create_session(
            patient_id=patient_id,
            doctor_id=doctor_id,
            locale=locale,
            consent=consent,
            snapshot=snapshot
        )
    # 4) Respond with the agreed contract (SummaryStartResponse)
    # FastAPI will serialize this back to JSON for the client.
    return SummaryStartResponse(
        session_id=sess.session_id,
        patient_id=sess.patient_id,
        doctor_id=sess.doctor_id,
        started_at=sess.started_at,
        status=sess.status,  # "collecting"
    )


def _pack_context(turns, max_chars: int = 8000) -> str: #latest conversation packaging for reasoning model
    """
    context packer:
    - Take the last N turns and flatten to text lines: 'Doctor: ...' / 'Assistant: ...'
    - Trim from the front to stay under a conservative char budget.
    """
    lines: List[str] = []
    for t in turns[-20:]:  # last 20 turns of conversation 
        who = "Doctor" if t.role == "doctor" else "Assistant"
        lines.append(f"{who}: {t.content}")
    ctx = "\n".join(lines).strip()
    if len(ctx) > max_chars:
        ctx = ctx[-max_chars:]
    return ctx



@router.post("/message", response_model=SummaryMessageResponse)
async def add_message(body: SummaryMessageRequest) -> SummaryMessageResponse:
    """
    Doctor adds a text note to the current session.

    This does NOT trigger reasoning — it's only for collecting input.

    Steps:
    1) Check session exists.
    2) Append the doctor's message to turns (role='doctor').
    3) Return how many total doctor messages exist (for optional UI feedback).
    """
     # 1️⃣ Validate session exists
    try:
        sess = add_doctor_message(body.session_id, body.text, modality="text")
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found")

    # 2️⃣ Count how many doctor messages exist (not assistant)
    total_doctor_msgs = sum(1 for t in sess.turns if t.role == "doctor")

    # 3️⃣ Return the response model
    return SummaryMessageResponse(
        session_id=body.session_id,
        total_messages=total_doctor_msgs
    )



@router.post("/reply", response_model=ResponseEnvelope)
async def summary_reply(body: SummaryMessageRequest) -> ResponseEnvelope:
    '''Flow:

        Doctor sends a short message; agent responds with speech_output.
    We append BOTH the doctor's turn and the assistant's turn to the session.
      
    1) Validate input (pydantic already validated types/required fields).
    2) Fetch the session by session_id, raise 404 if not found.
    3) Append the doctor's message to the session.
    4) Pack recent conversation context within a char limit.
    5) Call the reasoning client with the context and snapshot.
    6) Append the assistant's reply to the session.
    7) Return a ResponseEnvelope with the assistant's reply and reasoning data.'''

# 1) Append the doctor turn (404 if the session doesn't exist)
    try:
        sess = add_doctor_message(body.session_id, body.text, modality="text")
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found")
    

# 2) Build a small context window + include the snapshot
    context_text = _pack_context(sess.turns)
    snapshot = sess.snapshot
    locale = sess.locale

# 3) Call reasoning client (doctor-support persona)
    # Prefer a dedicated method. If you don't have it yet, still have something to  fall back on

    try:
        
        reply = await reason_client.generate_summary_reply(
            context=context_text,
            snapshot=snapshot,
            locale=locale,
        )

    # Expected fields: speech_output (str), intent (str, optional), confidence (float, optional)
        speech = reply["speech_output"] if isinstance(reply, dict) else getattr(reply, "speech_output", None)
        intent = reply.get("intent") if isinstance(reply, dict) else getattr(reply, "intent", None)
        conf = reply.get("confidence") if isinstance(reply, dict) else getattr(reply, "confidence", None)
        suggested = reply.get("suggested_actions") if isinstance(reply, dict) else getattr(reply, "suggested_actions", None)
    
    except AttributeError:
        # Fallback path if ReasoningClient doesn't yet have generate_summary_reply()
        try:
            # Synchronous or async depending on your client; adjust if needed
            rr = await reason_client.generate_reasoning(
                transcript=context_text,
                snapshot=snapshot,
                locale=locale,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Reasoning failed: {e}")
        # Map minimal fields for our envelope (MVP)
        speech = getattr(rr, "speech_output", None) or "Noted. What would you like me to clarify next?"
        intent = getattr(rr, "intent", None) or "answer"
        conf = getattr(rr, "confidence", None)
        suggested = getattr(rr, "suggested_actions", None)
        if suggested is None:
            suggested = ["present_conclusion", "keep_discussing", "show_soap_preview"]
        
        # 4) Store the assistant turn and light state
    add_assistant_reply(
        body.session_id,
        content=speech,
        modality="text",           # we store the text we would speak
        confidence=conf,
        intent=intent,
    )

    # 5) Return speech-first envelope (no UI in chat turns)
    return ResponseEnvelope(
        session_id=body.session_id,
        speech_output=speech,
        show_ui=False,
        ui=None,
        turns_appended=2,          # doctor + assistant
        intent=intent or "answer",
        confidence=conf,
        suggested_actions=suggested,
    )


@router.post("/finalize", response_model=ResponseEnvelope)
async def finalize_summary(body: SummaryFinalizeRequest) -> ResponseEnvelope:
    """
    Trigger reasoning over the collected notes + snapshot.
    Returns a SOAP preview (or saves if approve_save=True).
    """
    try:
        sess = get_session(body.session_id) # everythning is saved in the session 
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # 1️⃣ Generate SOAP JSON via reasoning

    try:
        js = await reason_client.generate_summary_finalize( 
            turns=sess.turns,
            snapshot=sess.snapshot,
            locale=sess.locale,
            preview_only=not body.approve_save
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Finalization failed: {e}")
    
    # 2️⃣ If preview → just return for review

    if not body.approve_save:
        mark_finalized(body.session_id)
        return ResponseEnvelope(
            session_id=body.session_id,
            speech_output=js["speech_output"],
            show_ui=True,
            ui={"soap": js["soap"]},  # 👈 wrap SOAP explicitly for the UI panel
            turns_appended=0,
            intent="show_preview",
            confidence=js.get("confidence"),
            suggested_actions=js.get("suggested_actions", ["approve_save", "reject_save"]),
        )
    
    # 3️⃣ If approved → save to DB via Zara’s service (stub now)
    try:
        visit_result = await save_visit_if_ready(sess, js)
        mark_saved(body.session_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB save failed: {e}")


    return ResponseEnvelope(
        session_id=body.session_id,
        speech_output="SOAP notes saved successfully.",
        show_ui=False,
        ui={"visit": visit_result},
        turns_appended=0,
        intent="confirm_finalize",
        confidence=1.0,
        suggested_actions=[],
    )
    



@router.post("/objective", response_model=ResponseEnvelope)
async def generate_objective(body: ObjectiveRequest) -> ResponseEnvelope:
    """
    Generate the Objective (Observation) section and require doctor approval before saving.

    Workflow:
      1) If approve_save=False → generate and preview Objective.
      2) If approve_save=True  → save approved Objective to DB and mark ready for DD agent.
    """

    try:
        sess = get_session(body.session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # 1️⃣ If it's just a preview, generate the Objective and return for review.
    if not body.approve_save:
        try:
            js = await reason_client.generate_objective_only(
                turns=sess.turns,
                snapshot=sess.snapshot,
                locale=sess.locale,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Objective generation failed: {e}")
        
        mark_finalized(body.session_id)  # Mark as finalized for review

        return ResponseEnvelope(
            session_id=body.session_id,
            speech_output=js["speech_output"],
            show_ui=True,
            ui={"objective": js["objective"]},
            turns_appended=0,
            intent="show_preview",
            confidence=None,
            suggested_actions=["approve_save", "reject_save"],
        )
    
    
    # 2️⃣ If the doctor approves, save and mark ready.
    try:
        visit_id = await save_visit_if_ready(sess, {"objective": "approved by doctor"})
        mark_saved(body.session_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB save failed: {e}")
    
    return ResponseEnvelope(
        session_id=body.session_id,
        speech_output="Objective section approved and saved for diagnostic processing.",
        show_ui=False,
        ui={"visit_id": visit_id},
        turns_appended=0,
        intent="confirm_finalize",
        confidence=1.0,
        suggested_actions=[],
    )


@router.post("/run", response_model=ResponseEnvelope)
async def summary_run(body: SummaryMessageRequest) -> ResponseEnvelope:
    """
    Unified endpoint that routes reasoning based on model intent or doctor instruction.
    Flow:
      1) Append doctor's message to session (so context includes it).
      2) Call reply mini-agent to get speech + intent + confidence.
      3) Branch on intent:
         - propose_objective/objective → generate Objective preview and return ui={"objective": ...}
         - propose_finalize/finalize   → generate SOAP preview and return ui={"soap": ...}
         - else                       → conversational turn only (show_ui=False)
      4) ALSO support simple "force" text triggers for demos (e.g., "preview soap").
    """

 # 1) Append doctor's message — if session_id invalid, raise 404
    try:
        sess = add_doctor_message(body.session_id, body.text, modality="text")
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found")

    # Pack latest turns for the model; also load snapshot + locale
    context_text = _pack_context(sess.turns)
    snapshot = sess.snapshot
    locale = sess.locale



    # 2) Ask the reply agent what to do next
    reply = await reason_client.generate_summary_reply(  # use the reply function above and gie it these inputs
        context=context_text,
        snapshot=snapshot,
        locale=locale
    )    

    intent = (reply.get("intent") or "answer").lower()   # give us these outputs 
    conf = reply.get("confidence")
    speech = reply.get("speech_output") or "Noted."
    suggested = reply.get("suggested_actions", [])


    #debug log for visisbility 
    print(f"[RUN] intent={intent} conf={conf} suggested={suggested}")

     # 2b) Force triggers via plain text (useful for demos)
    #     If the doctor types "show objective" or "preview soap", we force that branch.
    force_text = (body.text or "").lower()
    force_objective = any(k in force_text for k in ["show objective", "preview objective", "objective please"])
    force_finalize  = any(k in force_text for k in ["show soap", "preview soap", "soap please", "finalize"])

     # 3) Branch: Objective preview
    if intent in ("propose_objective", "objective") or force_objective:
        try:
            js_obj = await reason_client.generate_objective_only(
                turns=sess.turns,
                snapshot=snapshot,
                locale=locale
            )
        
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Objective generation failed: {e}")
        
        
        # Log assistant turn (what we'd speak)
        add_assistant_reply(
            body.session_id,
            content=js_obj.get("speech_output") or "Objective prepared for review.",
            modality="text",
            confidence=js_obj.get("confidence"),
            intent="objective"
        )


        return ResponseEnvelope(
            session_id=body.session_id,
            speech_output=js_obj.get("speech_output") or "Objective prepared for review.",
            show_ui=True,
            ui={"objective": js_obj.get("objective", "")},  # 👈 UI expects this key
            turns_appended=2,  # doctor + assistant
            intent="objective",
            confidence=js_obj.get("confidence"),
            suggested_actions=js_obj.get("suggested_actions", ["approve_save", "reject_save"]),
        )

    
    # 3) Branch: SOAP preview
    if intent in ("propose_finalize", "finalize") or force_finalize:
        try:
            js_soap = await reason_client.generate_summary_finalize(
                turns=sess.turns,
                snapshot=snapshot,
                locale=locale,
                preview_only=True
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"SOAP generation failed: {e}")

        # Log assistant turn (what we'd speak)
        add_assistant_reply(
            body.session_id,
            content=js_soap.get("speech_output") or "SOAP summary prepared for review.",
            modality="text",
            confidence=js_soap.get("confidence"),
            intent="finalize"
        )

        return ResponseEnvelope(
            session_id=body.session_id,
            speech_output=js_soap.get("speech_output") or "SOAP summary prepared for review.",
            show_ui=True,
            ui={"soap": js_soap.get("soap", {})},  # 👈 UI expects this key
            turns_appended=2,  # doctor + assistant
            intent="finalize",
            confidence=js_soap.get("confidence"),
            suggested_actions=js_soap.get("suggested_actions", ["approve_save", "reject_save"]),
        )

    # 4) Default: conversational turn only (no UI preview)
    add_assistant_reply(
        body.session_id,
        content=speech,
        modality="text",
        confidence=conf,
        intent=intent or "answer",
    )


    return ResponseEnvelope(
        session_id=body.session_id,
        speech_output=speech,
        show_ui=False,
        ui=None,
        turns_appended=2,  # doctor + assistant
        intent=intent or "answer",
        confidence=conf,
        suggested_actions=suggested or ["keep_discussing"],
    )
