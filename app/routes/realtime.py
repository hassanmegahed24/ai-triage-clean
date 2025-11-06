# app/routes/realtime.py
# -------------------------------------------------------------------
# Purpose:
#   - Issue a short-lived JWT for tool calls.
#   - The frontend (or the Realtime session bootstrap code) calls this
#     after /summary/start and stores it alongside session_id.
#   - Do NOT expose your OpenAI key here; this is ONLY for our tool API.
# -------------------------------------------------------------------
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import List
from app.services.summary_session import get_session
from app.utils.auth import sign_tool_jwt

router = APIRouter()

class ToolTokenRequest(BaseModel):
    session_id: str = Field(..., description="Active summary session id")
    scopes: List[str] = Field(default_factory=lambda: ["reply", "objective", "finalize"])

class ToolTokenResponse(BaseModel):
    session_id: str
    token: str


@router.post("/realtime/tool-token", response_model=ToolTokenResponse)
async def issue_tool_token(body: ToolTokenRequest) -> ToolTokenResponse:
    # Validate that session exists (prevents minting random tokens)
    try:
        _ = get_session(body.session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found")

    token = sign_tool_jwt(session_id=body.session_id, scopes=body.scopes)
    return ToolTokenResponse(session_id=body.session_id, token=token)



