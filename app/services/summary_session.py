# app/services/summary_session.py
# -------------------------------------------------------------------
# Purpose (MVP):
#   A tiny, swappable store for the Summary Agent. For now: in-memory.
#   Later: easily replaced by Redis/DB without touching the routes.
# -------------------------------------------------------------------

from __future__ import annotations

from typing import Dict, List, Optional, Literal
from datetime import datetime, timezone
from uuid import uuid4  # used later when we add create_session()
from pydantic import BaseModel, Field
from uuid import uuid4


from app.models.summary import MessageTurn, ResponseEnvelope
from app.models.transcript import Transcript
from app.utils.time import now_et

class SummarySession(BaseModel):
    '''
     Identifier:
      - session_id: opaque UUID for the session (set when created)
      - patient_id: EHR patient integer id (we locked this choice earlier)
      - doctor_id:  optional clinician/staff id for attribution
      - started_at: UTC timestamp when session was created
      - locale:     language hint for ASR/formatting (e.g., 'en', 'fr')
      - consent:    whether consent was recorded for this session

    Lifecycle:
      - status: 'collecting' | 'finalized' | 'saved'

    Collected content:
      - turns:       list of MessageTurn (doctor ↔ assistant dialogue)
      - transcripts: list of Transcript (normalized uploads)
      - snapshot:    patient snapshot dict cached at /summary/start

    Light conversation state (optional but useful):
      - current_confidence: float in [0,1] reported by assistant on last turn
      - last_intent:        last assistant intent string (e.g., 'ask', 'answer')
    '''

    session_id: str
    patient_id: int
    doctor_id: Optional[str] = None
    started_at: datetime
    status: Literal["collecting", "finalized", "saved"] = "collecting"
    consent: bool = True
    locale: str = "en"

    turns: List[MessageTurn] = Field(default_factory=list)
    transcripts: List[Transcript] = Field(default_factory=list)
    snapshot: dict = Field(default_factory=dict)
    working_notes: str = ""

    current_confidence: Optional[float] = None
    last_intent: Optional[str] = None



_SESSIONS: Dict[str, SummarySession] = {}  # class-level in-memory store for sessions

### creating mini functions to control that session state, create it, call it, add to it, etc.


def create_session(
    *,
    session_id: Optional[str] = None,
    patient_id: int,
    doctor_id: Optional[str],
    consent: bool,
    locale: str,
    snapshot: dict,
) -> SummarySession:
    """
    Create and register a new session. Called from /summary/start.
    Returns the validated SummarySession object.
    """
    sid = session_id or str(uuid4())
    sess = SummarySession(
        session_id=sid,
        patient_id=patient_id,
        doctor_id=doctor_id,
        started_at= now_et(),
        status="collecting",
        consent=consent,
        locale=locale or "en",
        snapshot=snapshot or {},
    )

    _SESSIONS[sid] = sess
    return sess

def get_session(session_id: str) -> SummarySession: 
    """
    Retrieve a session by its ID. Raises KeyError if not found.
    """
    sess = _SESSIONS.get(session_id)
    if not sess:
        raise KeyError(f"Session {session_id} not found")
    return sess

def add_doctor_message(session_id: str, text: str, modality: Literal["text", "voice"] = "text") -> SummarySession:
    
    """
    Append a doctor turn to the conversation.
    Stores the *text* even when modality='voice' (ASR-provided text).
    """
    sess = get_session(session_id)
    clean = (text or "").strip()
    if clean: #if there is text to add
        turn = MessageTurn(
            role="doctor",
            content=clean,
            modality=modality
        )
        sess.turns.append(turn)
    return sess

def add_assistant_reply(
    session_id: str,
    content: str,
    *,
    modality: Literal["text", "voice"] = "text",
    confidence: Optional[float] = None,
    intent: Optional[str] = None,
) -> SummarySession:
     """
    Append an assistant turn and (optionally) update light convo state.
    We typically store exactly what we *speak* back (speech-first).
    """
     sess = get_session(session_id)
     clean = (content or "").strip()
     if clean:
        turn = MessageTurn(role="assistant", content=clean, modality=modality)
        sess.turns.append(turn)
     if confidence is not None:
        sess.current_confidence = confidence
        if intent is not None:
            sess.last_intent = intent
     return sess
    

def add_transcript(session_id: str, t: Transcript) -> SummarySession:
    """
    Append a normalized Transcript produced from an upload
    (audio/video -> ASR; text/plain -> direct ingest).
    """
    sess = get_session(session_id)
    sess.transcripts.append(t)
    return sess

# Lifecycle helpers

def mark_finalized(session_id: str) -> SummarySession:
    """
    Mark the session as finalized (no more doctor input).
    Called from /summary/finalize.
    """
    sess = get_session(session_id)
    sess.status = "finalized"
    return sess

def mark_saved(session_id: str) -> SummarySession:
    """
    Mark the session as saved (summary stored in EHR).
    Called from /summary/save.
    """
    sess = get_session(session_id)
    sess.status = "saved"
    return sess


def set_working_notes(session_id: str, notes: str) -> SummarySession:
    """
    Overwrite the rolling working notes for this session.
    """
    sess = get_session(session_id)
    sess.working_notes = (notes or "").strip()
    return sess


def get_working_notes(session_id: str) -> str:
    try:
        sess = get_session(session_id)
    except KeyError:
        return ""
    return sess.working_notes

#test only cleanup/clear function

def _reset_sessions_for_tests() -> None:
    """
    Danger: test environments only.
    Clears all in-memory sessions to a blank slate.
    """
    _SESSIONS.clear()


    
