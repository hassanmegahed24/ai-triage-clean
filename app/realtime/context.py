import json
from typing import Any, Dict

import os
from app.clients.prompt_runner import render_system_instruction
from app.services.snapshot_builder import build_snapshot


def _compact_snapshot_for_rt(snap: Dict[str, Any]) -> Dict[str, Any]:
    """Return a concise patient context suitable for session instructions."""
    keep: Dict[str, Any] = {}
    # direct keys if present
    for k in ("patient",):
        if k in snap:
            keep[k] = snap.get(k)
    # trim common lists
    def _trim(name: str, limit: int) -> None:
        if name in snap and isinstance(snap[name], list):
            keep[name] = snap[name][:limit]

    for name, limit in (
        ("medical_history", 3),
        ("medications", 4),
        ("allergies", 4),
        ("labs", 4),
        ("diagnoses", 4),
    ):
        _trim(name, limit)

    return keep


def _hr_line(k: str, v: str) -> str:
    return f"- {k}: {v}"


def _human_readable_summary(compact: Dict[str, Any]) -> str:
    lines = []
    # Allergies
    allergies = compact.get("allergies") or []
    if isinstance(allergies, list) and allergies:
        names = []
        for a in allergies:
            if isinstance(a, dict):
                n = a.get("allergen") or a.get("name") or a.get("allergy")
                if n:
                    names.append(str(n))
        lines.append(_hr_line("Allergies", ", ".join(names) if names else "None/Not provided"))
    else:
        lines.append(_hr_line("Allergies", "None/Not provided"))

    # Past medical history
    mh = compact.get("medical_history") or []
    if isinstance(mh, list) and mh:
        details = []
        for m in mh:
            if isinstance(m, dict):
                cond = m.get("condition")
                status = m.get("status")
                if cond and status:
                    details.append(f"{cond} ({status})")
                elif cond:
                    details.append(str(cond))
        lines.append(_hr_line("Past medical history", ", ".join(details) if details else "Not provided"))
    else:
        lines.append(_hr_line("Past medical history", "Not provided"))

    # Medications
    meds = compact.get("medications") or []
    if isinstance(meds, list) and meds:
        names = []
        for m in meds:
            if isinstance(m, dict):
                n = m.get("medicine_name") or m.get("name") or m.get("drug")
                if n:
                    names.append(str(n))
        lines.append(_hr_line("Medications", ", ".join(names) if names else "Not provided"))
    else:
        lines.append(_hr_line("Medications", "Not provided"))

    # Diagnoses (recent)
    dx = compact.get("diagnoses") or []
    if isinstance(dx, list) and dx:
        names = []
        for d in dx:
            if isinstance(d, dict):
                n = d.get("diagnosis_description") or d.get("diagnosis")
                if n:
                    names.append(str(n))
        lines.append(_hr_line("Diagnoses", ", ".join(names) if names else "Not provided"))
    else:
        lines.append(_hr_line("Diagnoses", "Not provided"))

    return "\n".join(lines)


async def make_session_instructions(patient_id: int, system_file: str = "system_global.txt") -> str:
    """Compose session instructions for Realtime using system prompt + compact snapshot.

    Rules are in English by default. The agent should reply in English unless
    the user clearly speaks another language, then adapt accordingly.
    """
    system_text = render_system_instruction(system_file)
    # If a default language is provided via env, bias outputs toward it.
    # e.g., REALTIME_DEFAULT_LANGUAGE=zh or zh-CN
    default_lang = (os.getenv("REALTIME_DEFAULT_LANGUAGE") or "").strip()
    if default_lang:
        system_text = system_text.rstrip() + f"\n\nLanguage: reply in {default_lang} unless the user asks otherwise."
    snap = await build_snapshot(patient_id)
    compact = _compact_snapshot_for_rt(snap)
    context_json = json.dumps(compact, ensure_ascii=False)
    readable = _human_readable_summary(compact)

    strict_rules = (
        "Please follow these rules strictly:\n"
        "1) Use ONLY the Known patient context below; do NOT invent facts.\n"
        "2) If something is missing, say 'unknown' rather than guessing.\n"
        "3) When the user explicitly asks to 'quote/verbatim' a field, output the exact original text without modification and without any prefix/suffix or code fences.\n"
        "4) Do not proactively recite the entire patient context; use it internally for reasoning and safety unless the user asks to recite.\n"
        "5) Keep responses concise, professional, and safe. \n"
    )

    return (
        f"{system_text}\n\n"
        f"{strict_rules}\n"
        f"Known patient context (JSON):\n{context_json}\n\n"
        f"Summary:\n{readable}\n"
    )
