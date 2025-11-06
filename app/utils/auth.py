# app/utils/auth.py
# -------------------------------------------------------------------
# Purpose:
#   - Issue and verify short-lived JWTs for tool endpoints.
#   - We include session_id + allowed scopes inside the token.
#   - Realtime tool calls must present this token as: Authorization: Bearer <token>
#
# Env:
#   TOOL_JWT_SECRET    - HMAC secret (set in .env; dev-safe random string)
#   TOOL_JWT_TTL_SEC   - default 300s (5 min) tokens
# -------------------------------------------------------------------
import os, time, hmac, hashlib, base64, json
from typing import List, Dict, Any


SECRET = os.getenv("TOOL_JWT_SECRET", "dev-only-change-me")
TTL    = int(os.getenv("TOOL_JWT_TTL_SEC", "300"))

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

def _b64url_json(obj: Dict[str, Any]) -> str:
    return _b64url(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def sign_tool_jwt(*, session_id: str, scopes: List[str]) -> str:
    """Create a compact HS256 JWT with {session_id, scopes, exp}."""
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sid": session_id,
        "scp": scopes,
        "exp": int(time.time()) + TTL
    }
    seg1 = _b64url_json(header)
    seg2 = _b64url_json(payload)
    signing_input = f"{seg1}.{seg2}".encode("ascii")
    sig = hmac.new(SECRET.encode("utf-8"), signing_input, hashlib.sha256).digest()
    seg3 = _b64url(sig)
    return f"{seg1}.{seg2}.{seg3}"

def verify_tool_jwt(token: str) -> Dict[str, Any]:
    """Return decoded payload if valid, else raise ValueError."""
    try:
        seg1, seg2, seg3 = token.split(".")
    except ValueError:
        raise ValueError("Malformed token")
    signing_input = f"{seg1}.{seg2}".encode("ascii")
    expected = hmac.new(SECRET.encode("utf-8"), signing_input, hashlib.sha256).digest()
    given = base64.urlsafe_b64decode(seg3 + "==")
    if not hmac.compare_digest(expected, given):
        raise ValueError("Invalid signature")
    payload = json.loads(base64.urlsafe_b64decode(seg2 + "==").decode("utf-8"))
    if int(time.time()) > int(payload.get("exp", 0)):
        raise ValueError("Token expired")
    return payload  # contains: sid (session_id), scp (scopes)