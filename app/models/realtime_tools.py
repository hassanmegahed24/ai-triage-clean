# app/models/realtime_tools.py
from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Literal, Optional, Dict, Any, List


# ---- Common inputs every tool needs
class ToolBaseInput(BaseModel):
    session_id: str = Field(..., description="Active summary session id")
    locale: Optional[str] = Field(None, description="Language hint, e.g., 'en'")


# ---- summary_reply (discovery loop)
class SummaryReplyInput(ToolBaseInput):
    # The Realtime model will pass the most recent doctor utterance;
    # our handler will also pull full context from the session.
    latest_user_text: str = Field(..., description="Recent doctor utterance")


class SummaryReplyOutput(BaseModel):
    speech_output: str
    intent: Literal["ask", "answer", "propose_objective", "propose_finalize"]
    confidence: float = Field(..., ge=0.0, le=1.0)
    suggested_actions: List[Literal["keep_discussing", "show_objective_preview", "show_soap_preview"]] = []


# ---- summary_objective (preview Objective)
class SummaryObjectiveInput(ToolBaseInput):
    pass  # context comes from session

class SummaryObjectiveOutput(BaseModel):
    objective: str
    speech_output: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    suggested_actions: List[Literal["approve_save", "reject_save"]] = []

# ---- summary_finalize (preview SOAP)
class SummaryFinalizeInput(ToolBaseInput):
    preview_only: bool = True

class SoapPayload(BaseModel):
    subjective: str = ""
    objective: str = ""
    assessment: str = ""
    plan: str = ""

class SummaryFinalizeOutput(BaseModel):
    soap: SoapPayload
    next_steps: List[str] = []
    speech_output: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    suggested_actions: List[Literal["approve_save", "reject_save"]] = []
