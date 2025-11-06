# app/clients/reasoning_client.py
# ------------------------------------------------------------
# Reasoning Client — unified interface for Summary Agent tasks.
# This file centralizes GPT calls and ensures consistent return
# structures for the routes/UI (ResponseEnvelope).
#
# Key changes vs your previous version:
#  - Prompts are loaded from /app/prompts/*.txt (system + user)
#  - Each method returns a normalized dict with the exact keys
#    the routes/Streamlit expect (e.g., "objective", "soap")
#  - A single AsyncOpenAI client is used across all methods
#  - Safe JSON parsing and minimal debug logging are included
#
# NOTE (for Stacy / S2S integration):
#  - We always include "speech_output" in the return dict.
#    This is the TTS-ready text your tts.py can speak.
#  - When you add streaming, you can hook into these methods
#    by streaming the same "speech_output" text to TTS.
# ------------------------------------------------------------

import os, json
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI
from pydantic import ValidationError

# We keep using your prompt loader utilities
from app.models.reasoning import ReasoningResponse
from app.utils.prompt_loader import load_prompt, render_prompt

# One shared async client (key from env)
# We’ll also assign it to self.client inside the class for clarity/consistency
_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# --------------------------
# Small local helpers
# --------------------------
def _pack_turns(turns: List[Any], limit: int = 50) -> str:
    """
    Flatten the last N turns into 'Role: content' lines for prompts.
    Turns are expected to have attributes .role and .content
    """
    lines = [f"{t.role.title()}: {t.content}" for t in turns[-limit:]]
    return "\n".join(lines)

def _safe_json_loads(raw: str) -> Dict[str, Any]:
    """
    Parse JSON safely from the model. If parsing fails, return {}.
    """
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


class ReasoningClient:
    def __init__(self, model: str = "gpt-4o-mini"):
        self.model = model
        # Global system (if you need a global prefix); we keep it as-is.
        self.system_prompt = load_prompt("system_global.txt")
        # Use a single client instance
        self.client = _client

    # ------------------------------------------------------------
    # Generic reasoning (legacy/fallback) — kept intact for now
    # ------------------------------------------------------------
    async def generate_reasoning(self, transcript: str, snapshot: dict) -> ReasoningResponse:
        """
        Legacy/fallback generic reasoning call.
        NOTE: Kept as-is so nothing else breaks; we only fixed the missing 'await'.
        """
        user_prompt = (
            f'Transcript: """{transcript}"""\n\n'
            f'Snapshot: """{json.dumps(snapshot, indent=2)}"""\n\n'
        )

        # ✅ ensure we await the async call
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.2,
            response_format={"type": "json_object"}  # enforce JSON output
        )

        json_output = response.choices[0].message.content

        try:
            # validate into our Pydantic schema
            return ReasoningResponse.model_validate_json(json_output)
        except ValidationError as e:
            raise RuntimeError(f"Response validation failed: {e}\n\nRaw: {json_output}")

    # ------------------------------------------------------------
    # Summary REPLY — conversational loop (intent + speech_output)
    # ------------------------------------------------------------
    async def generate_summary_reply(
        self,
        *,
        context: str,              # already-packed short conversation text
        snapshot: dict,            # patient snapshot
        locale: str = "en",
    ) -> Dict[str, Any]:
        """
        Generates a concise, speech-first reply for the doctor-support agent.

        Returns (normalized):
          {
            "speech_output": str,
            "intent": "ask"|"answer"|"propose_objective"|"propose_finalize",
            "confidence": float (0–1),
            "suggested_actions": list[str]
          }

        TTS NOTE:
          - "speech_output" is the exact string your TTS can speak.
        """

        # 1) Load prompts from files (system + user)
        system_prompt = load_prompt("summary_reply.system.txt")
        user_template = load_prompt("summary_reply.user.txt")

        # 2) Fill user template with context and snapshot
        user_prompt = render_prompt(
            user_template,
            turns=context,  # we accept 'context' as the packed turn text
            snapshot=json.dumps(snapshot, indent=2),
            locale=locale
        )

        # 3) Call OpenAI
        response = await self.client.chat.completions.create(
            model=self.model,
            temperature=0.6,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt.strip()},
                {"role": "user", "content": user_prompt.strip()},
            ],
        )

        # 4) Parse and normalize
        raw = response.choices[0].message.content
        js = _safe_json_loads(raw)

        speech = js.get("speech_output") or "Noted. What would you like to clarify next?"
        intent = (js.get("intent") or "answer").lower()
        try:
            confidence = float(js.get("confidence", 0.5))
        except Exception:
            confidence = 0.5
        suggested = js.get("suggested_actions") or ["keep_discussing"]

        # Minimal debug for turn-by-turn tracing
        print(f"[REPLY] intent={intent} conf={confidence} keys={list(js.keys())}")

        return {
            "speech_output": speech,
            "intent": intent,
            "confidence": confidence,
            "suggested_actions": suggested
        }

    # ------------------------------------------------------------
    # Objective ONLY — produce Observation (preview)
    # ------------------------------------------------------------
    async def generate_objective_only(
        self,
        *,
        turns: list,               # list of MessageTurn
        snapshot: dict,
        locale: str = "en",
    ) -> Dict[str, Any]:
        """
        Produce ONLY the Objective/Observation section (text),
        plus a short speech_output that says what's ready.

        Returns (normalized):
          {
            "objective": str,
            "speech_output": str,
            "confidence": float (0–1),
            "suggested_actions": list[str]
          }
        """

        # 1) Load prompts from files (system + user)
        system_prompt = load_prompt("summary_objective.system.txt")
        user_template = load_prompt("summary_objective.user.txt")

        # 2) Pack recent turns + snapshot into user prompt
        turns_text = _pack_turns(turns, limit=50) 
        user_prompt = render_prompt(
            user_template,
            turns=turns_text,
            snapshot=json.dumps(snapshot, indent=2),
            locale=locale
        )

        # 3) Call OpenAI
        resp = await self.client.chat.completions.create(
            model=self.model,
            temperature=0.6,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt.strip()},
                {"role": "user", "content": user_prompt.strip()},
            ],
        )

        # 4) Parse and normalize
        raw = resp.choices[0].message.content
        js = _safe_json_loads(raw)

        objective = js.get("objective") or "No objective data extracted."
        speech = js.get("speech_output") or "Objective drafted; please review on screen."
        try:
            confidence = float(js.get("confidence", 0.8))
        except Exception:
            confidence = 0.8
        suggested = js.get("suggested_actions") or ["approve_save", "reject_save"]

        print(f"[OBJECTIVE] conf={confidence} keys={list(js.keys())}")

        return {
            "speech_output": speech,
            "objective": objective,      # <- CRUCIAL for UI
            "confidence": confidence,
            "suggested_actions": suggested
        }

    # ------------------------------------------------------------
    # Finalize — produce SOAP (preview or final)
    # ------------------------------------------------------------
    async def generate_summary_finalize(
        self,
        *,
        turns: list,               # list of MessageTurn
        snapshot: dict,
        locale: str = "en",
        preview_only: bool = True
    ) -> Dict[str, Any]:
        """
        Generate a structured SOAP summary for the doctor to review.

        Inputs:
          - turns: list of MessageTurn (conversation and notes)
          - snapshot: structured patient snapshot
          - locale: language (default: 'en')
          - preview_only: if True → preview JSON for review
                          if False → this is the final approved save

        Normalized return:
          {
            "speech_output": str,
            "soap": { "subjective": "", "objective": "", "assessment": "", "plan": "" },
            "confidence": float (0–1),
            "suggested_actions": list[str]
          }

        TTS NOTE:
          - "speech_output" is what your TTS should speak when announcing the preview.
        """

        # 1) Load prompts (system + user)
        system_prompt = load_prompt("summary_finalize.system.txt")
        user_template = load_prompt("summary_finalize.user.txt")

        # 2) Build user prompt from turns + snapshot
        turns_text = _pack_turns(turns, limit=50)
        user_prompt = render_prompt(
            user_template,
            turns=turns_text,
            snapshot=json.dumps(snapshot, indent=2),
            preview_only=str(preview_only).lower(),
            locale=locale
        )

        # 3) Call OpenAI
        response = await self.client.chat.completions.create(
            model=self.model,
            temperature=0.6,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt.strip()},
                {"role": "user", "content": user_prompt.strip()},
            ],
        )

        # 4) Parse + normalize for UI consumption
        raw = response.choices[0].message.content
        js = _safe_json_loads(raw)

        # Extract or coerce SOAP into proper dict shape
        soap = js.get("soap") or {}
        if not isinstance(soap, dict):
            # If model returned a string by mistake, wrap minimally
            soap = {
                "subjective": str(soap),
                "objective": "",
                "assessment": "",
                "plan": ""
            }

        # Defaults
        js.setdefault("next_steps", [])
        js.setdefault("speech_output", "SOAP summary prepared.")
        try:
            confidence = float(js.get("confidence", 0.9))
        except Exception:
            confidence = 0.9
        suggested = js.get("suggested_actions") or ["approve_save", "reject_save"]

        print(f"[FINALIZE] conf={confidence} keys={list(js.keys())}")

        return {
            "speech_output": js["speech_output"],
            "soap": soap,                   # <- CRUCIAL for UI rendering
            "confidence": confidence,
            "suggested_actions": suggested
        }
