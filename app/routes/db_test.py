# app/routes/db_test.py
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Any, Dict
from app.services.db_writer import write_feedback_row

router = APIRouter(prefix="/db", tags=["database"])

class FeedbackIn(BaseModel):
    patient_id: int
    treatment: str
    feedback: str
    datetime_iso: str
    is_severe: bool
    feedback_type: str

@router.post("/write")
async def db_write(feedback: FeedbackIn) -> Dict[str, Any]:
    return await write_feedback_row(
        patient_id=feedback.patient_id,
        treatment=feedback.treatment,
        feedback=feedback.feedback,
        datetime_iso=feedback.datetime_iso,
        is_severe=feedback.is_severe,
        feedback_type=feedback.feedback_type,
    )