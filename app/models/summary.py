from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime
from app.models.transcript import Transcript
from app.models.reasoning import ReasoningResponse
from app.utils.time import now_et
from uuid import uuid4
from typing import Any, Dict, List



# -----------------------------
# Conversation support
# -----------------------------
class MessageTurn(BaseModel):
    """
    One conversation turn in the Summary Agent session.

    Fields:
    - role:       who spoke ('doctor' or 'assistant')
    - content:    canonical text (typed by doctor OR ASR result)
    - modality:   'text' or 'voice' (even for voice, we store recognized text)
    - timestamp:  UTC timestamp when the turn was added
    """
    role: Literal["doctor", "assistant"]
    content: str = Field(min_length=1)
    modality: Literal["text", "voice"] = "text"
    timestamp: datetime = Field(default_factory=now_et)

class ResponseEnvelope(BaseModel):
    """
    Standard wrapper for ALL Summary Agent responses (speech-first).

    Fields:
    - session_id:     for client correlation
    - speech_output:  what TTS should speak immediately
    - show_ui:        if True, client should render 'ui'
    - ui:             optional visual payload (dict or typed object),
                      e.g., {'soap': {...}}, {'questions': [...]}, {'preview': ReasoningResponse}
    - turns_appended: optional count of new turns appended by this call
    """
    session_id: str
    speech_output: str = Field(min_length=1)
    show_ui: bool = False
    ui: Optional[Dict[str, Any]] = None
    turns_appended: Optional[int] = None

    # conversational control
    intent: Optional[str] = None
    confidence: Optional[float] = None
    suggested_actions: Optional[List[str]] = None


class SummaryStartRequest(BaseModel):
    """
    Doctor starts a summarization session for a given patient.

    Fields:
    - patient_id:   The EHR patient identifier (int or str, depending on your DB).
    - doctor_id:    Optional clinician identifier (string user id or staff id).
    - locale:       Language hints (e.g., 'en', 'fr'); affects ASR/formatting.
    - consent:      Whether consent has been recorded for this session.
    """

    patient_id: int
    doctor_id: Optional[str] = None
    locale: Optional[str] = Field(None, description="Language code (e.g., 'en', 'fr')")
    consent: bool = True

class SummaryStartResponse(BaseModel):
    """
    Returned by /summary/start after creating a new session.

    Fields:
    - session_id:   Opaque UUID for this in-memory session.
    - patient_id:   Echo of input for client convenience.
    - doctor_id:    Echo of input for client convenience.
    - started_at:   UTC timestamp when session was created.
    - status:       Current session state: collecting | finalized | saved.
    """
    session_id : str
    patient_id: int
    doctor_id: Optional[str] = None
    started_at: datetime
    status: Literal["collecting", "finalized", "saved"] = "collecting"

class SummaryMessageRequest(BaseModel):
    """
    Append a short free-text note from the doctor to the session.

    Fields:
    - session_id:   The session to attach this message to.
    - text:         The actual note (validated to be non-empty).
    """
    session_id: str
    text: str = Field(min_length=1, description="Doctor note to add to the session") 

class SummaryMessageResponse(BaseModel):
   """
    Acknowledge the message append.

    Fields:
    - session_id:       Echo for client correlation.
    - total_messages:   How many free-text messages are now stored.
    """
   session_id: str
   total_messages: int

class SummaryUploadResponse(BaseModel):
    """
    The result of uploading a file (text/audio/video) to the session.

    Fields:
    - session_id:         Echo for client correlation.
    - transcript:         Normalized Transcript object derived from the file.
    - filename:           Original filename (if provided by client/OS).
    - total_transcripts:  How many transcripts are now stored in the session.
    """
    session_id: str
    transcript: Transcript
    filename: str
    total_transcripts: int

class SummaryFinalizeRequest(BaseModel):
    """
    Trigger reasoning over the collected notes+transcripts+snapshot.

    Fields:
    - session_id:   Which session to finalize.
    - approve_save: If false -> preview only (no DB write).
                    If true  -> persist to DB (via visit_writer) after validation.
    """
   
# -----------------------------
# Finalization endpoint
# -----------------------------
class SummaryFinalizeRequest(BaseModel):
    """
    Trigger reasoning over collected notes + transcripts + snapshot.

    Fields:
    - session_id:   which session to finalize
    - approve_save: False -> preview only (no DB write)
                    True  -> persist to DB (via visit_writer) after validation
    """
    session_id: str
    approve_save: bool = False

class SummaryFinalizeResponse(BaseModel):
    """
    Returned by /summary/finalize.

    Fields:
    - session_id:   echo for client correlation
    - preview:      structured reasoning output (ReasoningResponse)
    - saved:        False -> preview only; True -> persisted successfully
    - visit_id:     identifier of the saved visit (if saved=True)
    - status:       'finalized' or 'saved'
    """
    session_id: str
    preview: ReasoningResponse
    saved: bool = False
    visit_id: Optional[str] = None
    status: Literal["collecting", "finalized", "saved"] = "finalized"

# -----------------------------
# mini functions for eacch state change
# -----------------------------
class ObjectiveRequest(BaseModel):
    session_id: str
    approve_save: bool = False  # doctor toggles this after reviewing