# app/services/visit_logger.py
import os
from typing import Dict, Any
import httpx
from dotenv import load_dotenv

load_dotenv()

E_HOSPITAL_BASE_URL = os.getenv("E_HOSPITAL_BASE_URL", "https://aetab8pjmb.us-east-1.awsapprunner.com")
VISIT_LOG_TABLE = os.getenv("VISIT_LOG_TABLE", "patient_feedback")
VISIT_LOG_API_KEY = os.getenv("VISIT_LOG_API_KEY", "")  # optional


class VisitLoggerService:
    """Service for logging patient feedback entries into the E-Hospital Database via REST API."""

    def __init__(self):
        if not E_HOSPITAL_BASE_URL:
            raise RuntimeError("E_HOSPITAL_BASE_URL not set in .env")

        self.base_url = E_HOSPITAL_BASE_URL.rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        if VISIT_LOG_API_KEY:
            self.headers["apikey"] = VISIT_LOG_API_KEY

    async def log_feedback(
        self,
        patient_id: int,
        treatment: str,
        feedback: str,
        datetime_iso: str,
        is_severe: bool,
        feedback_type: str,
    ) -> Dict[str, Any]:
        """
        Insert a feedback record into the patient_feedback table using POST /table/<table_name>.
        """
        url = f"{self.base_url}/table/{VISIT_LOG_TABLE}"
        row = {
            "patient_id": patient_id,
            "treatment": treatment,
            "feedback": feedback,
            "datetime": datetime_iso,
            "is_severe": str(is_severe).lower(),  # store as "true"/"false"
            "feedback_type": feedback_type,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=row, headers=self.headers)

        ctype = resp.headers.get("Content-Type", "")
        try:
            body = resp.json() if ctype.startswith("application/json") else resp.text
        except Exception:
            body = resp.text

        return {
            "ok": resp.status_code in (200, 201),
            "status_code": resp.status_code,
            "url": url,
            "payload": row,
            "body": body,
        }