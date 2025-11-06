# app/services/db_writer.py
from __future__ import annotations

import os
from typing import Any, Dict, Optional
import httpx

DB_WRITE_URL = os.getenv("DB_WRITE_URL") or ""
DB_API_KEY  = os.getenv("DB_API_KEY") or ""

async def write_feedback_row(
    *,
    patient_id: int,
    treatment: str,
    feedback: str,
    datetime_iso: str,   # "2025-06-11 22:00:00"
    is_severe: bool,
    feedback_type: str,
) -> Dict[str, Any]:
    if not DB_WRITE_URL:
        return {"ok": False, "where": "db_writer", "error": "DB_WRITE_URL missing in env"}

    payload = {
        "patient_id": patient_id,
        "treatment": treatment,
        "feedback": feedback,
        "datetime": datetime_iso,
        # If their API wants true/false literals, keep as bool.
        # If it wants strings ("true"/"false"), flip to str here:
        "is_severe": is_severe,
        "feedback_type": feedback_type,
    }

    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if DB_API_KEY.strip():
        # adjust if they told you to use a different header
        headers["x-api-key"] = DB_API_KEY.strip()
        # or: headers["Authorization"] = f"Bearer {DB_API_KEY.strip()}"

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(DB_WRITE_URL, json=payload, headers=headers)

    # Try to decode JSON; fall back to raw text for debugging
    try:
        body: Any = resp.json()
    except Exception:
        body = await resp.aread()

    return {
        "ok": resp.status_code in (200, 201),
        "status_code": resp.status_code,
        "url": DB_WRITE_URL,
        "payload": payload,
        "body": body,
        "headers_used": list(headers.keys()),
    }