# app/routes/tools.py
# -------------------------------------------------------------------
# Purpose:
#   - HTTPS endpoints that the OpenAI Realtime "tool calls" will hit.
#   - They simply:
#       1) verify Authorization: Bearer <tool_jwt>
#       2) check scope
#       3) forward to our already-built handlers
#   - Outputs are strict JSON that match the Realtime tool schemas.
# -------------------------------------------------------------------

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any

from app.utils.auth import verify_tool_jwt
from app.services.realtime_tool_handlers import (
    handle_summary_reply,
    handle_summary_objective,
    handle_summary_finalize
)


router = APIRouter()

# ---- helper ---------------------------------------------------------
def _require_auth(auth_header: Optional[str], needed_scope: str, session_id: str):
    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization Bearer token")
    token = auth_header.split(" ", 1)[1].strip()
    try:
        payload = verify_tool_jwt(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=f"Auth failed: {e}")
    if payload.get("sid") != session_id:
        raise HTTPException(status_code=403, detail="Session mismatch")
    if needed_scope not in (payload.get("scp") or []):
        raise HTTPException(status_code=403, detail=f"Scope '{needed_scope}' required")
    return True



# ---- request bodies -------------------------------------------------
class ReplyBody(BaseModel):
    session_id: str
    latest_user_text: str
    locale: Optional[str] = "en"

class ObjectiveBody(BaseModel):
    session_id: str
    locale: Optional[str] = "en"

class FinalizeBody(BaseModel):
    session_id: str
    preview_only: bool = True
    locale: Optional[str] = "en"

# ---- endpoints ------------------------------------------------------
@router.post("/tools/reply")
async def tools_reply(body: ReplyBody, authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization, needed_scope="reply", session_id=body.session_id)
    try:
        out = await handle_summary_reply(body.dict())
        return out
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tool 'reply' failed: {e}")


@router.post("/tools/objective")
async def tools_objective(body: ObjectiveBody, authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization, needed_scope="objective", session_id=body.session_id)
    try:
        out = await handle_summary_objective(body.dict())
        return out
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tool 'objective' failed: {e}")


@router.post("/tools/finalize")
async def tools_finalize(body: FinalizeBody, authorization: Optional[str] = Header(default=None)):
    _require_auth(authorization, needed_scope="finalize", session_id=body.session_id)
    try:
        out = await handle_summary_finalize(body.dict())
        return out
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tool 'finalize' failed: {e}")
    


