"""Proxy endpoints for the E-Hospital AppRunner database."""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/db", tags=["db"])


def get_app_runner_base() -> str:
    """Resolve and validate the upstream AppRunner base URL."""
    base = (os.getenv("E_HOSPITAL_BASE_URL") or "").strip().rstrip("/")
    if not base:
        raise HTTPException(status_code=500, detail="E_HOSPITAL_BASE_URL missing")
    if "127.0.0.1" in base or "localhost" in base:
        raise HTTPException(
            status_code=500,
            detail=(
                f"E_HOSPITAL_BASE_URL points to local backend ({base}). "
                "Set it to your AWS AppRunner base, e.g. https://aetab8pjmb.us-east-1.awsapprunner.com"
            ),
        )
    return base


def _parse_dt(value: str) -> datetime:
    if not value:
        return datetime.min
    text = str(value).replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return datetime.min


@router.get("/debug")
def debug_env() -> dict:
    return {"E_HOSPITAL_BASE_URL": os.getenv("E_HOSPITAL_BASE_URL", "")}


@router.get("/patient_feedback")
async def get_patient_feedback(
    ymd: Optional[str] = Query(None, description="Optional YYYY-MM-DD filter"),
    limit: int = Query(10, ge=1, le=200),
):
    """Return up to `limit` patient feedback rows, newest first."""

    base = get_app_runner_base()
    url = f"{base}/table/patient_feedback"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
        resp.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AWS fetch failed: {e}")

    ctype = resp.headers.get("content-type", "")
    raw = resp.json() if "application/json" in ctype else []
    rows = raw.get("data", raw) if isinstance(raw, dict) else raw

    sorted_rows = sorted(
        rows,
        key=lambda row: _parse_dt(row.get("datetime") or row.get("created_at") or row.get("timestamp")),
        reverse=True,
    )

    if not ymd:
        return sorted_rows[:limit]

    normalized = ymd.strip()
    primary = [
        row
        for row in sorted_rows
        if str(row.get("datetime", "") or row.get("created_at", "")).replace("T", " ").startswith(normalized)
    ]

    if len(primary) >= limit:
        return primary[:limit]

    seen = {id(row) for row in primary}
    fallback = [row for row in sorted_rows if id(row) not in seen]
    combined = primary + fallback
    return combined[:limit]


@router.post("/patient_feedback")
async def post_patient_feedback(payload: dict):
    base = get_app_runner_base()
    url = f"{base}/table/patient_feedback"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
        body = resp.json() if "application/json" in resp.headers.get("content-type", "") else resp.text
        return {"status_code": resp.status_code, "body": body}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AWS write failed: {e}")
