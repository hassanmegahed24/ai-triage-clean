from __future__ import annotations

from typing import Any, Dict

from app.services.visit_logger import VisitLoggerService
from app.utils.time import now_et


visit_logger = VisitLoggerService()


def _to_text(block: Any) -> str:
    """Flatten strings/lists from the SOAP payload."""
    if block is None:
        return ""
    if isinstance(block, str):
        return block.strip()
    if isinstance(block, (list, tuple)):
        return "\n".join(str(item).strip() for item in block if str(item).strip())
    return str(block)


async def save_visit_if_ready(sess, js) -> Dict[str, Any]:
    """
    Persist the finalized SOAP/Observation into the E-Hospital patient_feedback table.

    Returns the VisitLoggerService response (status, payload, body).
    Raises RuntimeError if the upstream call fails.
    """

    soap = js.get("soap", {})

    subjective = _to_text(soap.get("subjective"))
    objective = _to_text(soap.get("objective"))
    assessment = _to_text(soap.get("assessment"))
    plan = _to_text(soap.get("plan"))

    # Working notes saved via save_observation are stored on the session
    observation = getattr(sess, "working_notes", "") or ""

    # Compose a concise feedback block for downstream analytics
    feedback_parts = [
        part for part in (
            "Subjective: " + subjective if subjective else "",
            "Objective: " + objective if objective else "",
            "Assessment: " + assessment if assessment else "",
            "Observation: " + observation if observation else "",
        )
        if part
    ]
    feedback_text = "\n\n".join(feedback_parts)

    treatment_text = plan or "Awaiting physician plan"

    result = await visit_logger.log_feedback(
        patient_id=sess.patient_id,
        treatment=treatment_text,
        feedback=feedback_text or "SOAP summary generated",
        datetime_iso=now_et().strftime("%Y-%m-%d %H:%M:%S"),
        is_severe=False,
        feedback_type="soap_summary",
    )

    if not result.get("ok"):
        raise RuntimeError(f"Visit save failed: {result}")

    return result
