# app/routes/visits.py
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from datetime import datetime
from typing import Dict, Any

from app.services.visit_logger import VisitLoggerService

router = APIRouter(prefix="/visits", tags=["visits"])
visit_logger = VisitLoggerService()


# Payload that matches the `patient_feedback` table
class FeedbackLogRequest(BaseModel):
    patient_id: int
    treatment: str
    feedback: str
    # accepts "2025-10-09 22:00:00" and other ISO-like strings
    datetime: datetime
    is_severe: bool
    feedback_type: str


# Keep the old path if you like; it now logs to patient_feedback
@router.post("/log-visit")
async def create_feedback_log(body: FeedbackLogRequest) -> Dict[str, Any]:
    try:
        # pass values through; format datetime exactly as table expects
        return await visit_logger.log_feedback(
            patient_id=body.patient_id,
            treatment=body.treatment,
            feedback=body.feedback,
            datetime_iso=body.datetime.strftime("%Y-%m-%d %H:%M:%S"),
            is_severe=body.is_severe,
            feedback_type=body.feedback_type,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))