# app/utils/prompt_loader.py
# ------------------------------------------------------------
# Prompt loader + renderer utilities
#
# This module now supports **both** calling styles for render_prompt:
#
#   1) New style (used by reasoning_client.py now):
#       render_prompt(template_str, turns=..., snapshot=..., locale=..., preview_only=...)
#
#   2) Legacy style (older code in the repo can keep working):
#       render_prompt(system_content, task_content, context_dict)
#       -> combines system + task with a separator and renders placeholders
#
# Placeholders use the form:  {{ variable_name }}
# Dict/List values are pretty-printed as JSON automatically.
# ------------------------------------------------------------

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

# Match {{ var }} with optional whitespace
_VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

def load_prompt(filename: str) -> str:
    """
    Load a single prompt file from app/prompts/<filename>.
    """
    path = PROMPTS_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Prompt file {filename} not found in {PROMPTS_DIR}")
    return path.read_text(encoding="utf-8")

def load_system_prompt() -> str:
    """
    Convenience wrapper for the single global system prompt.
    """
    return load_prompt("system_global.txt")

def load_task_prompt(task_name: str) -> str:
    """
    Load a specific task prompt.
    You pass only the task name (e.g., "soap", "questions", "differential", "next_actions"),
    and this function maps it to the file "task_<name>.txt".
    (Kept for backward-compat usage if present elsewhere.)
    """
    filename = f"task_{task_name}.txt"
    return load_prompt(filename)

# --------------------------
# Internal helpers
# --------------------------
def _jsonify(val: Any) -> str:
    """
    Convert dict/list to pretty JSON; otherwise cast to str.
    """
    if isinstance(val, (dict, list)):
        return json.dumps(val, indent=2, ensure_ascii=False)
    return str(val)

def _apply_aliases(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Simple aliasing: if a caller provides 'ehr_json' but not 'snapshot',
    auto-fill 'snapshot' to simplify prompt templates.
    """
    if "ehr_json" in ctx and "snapshot" not in ctx:
        ctx = {**ctx, "snapshot": ctx["ehr_json"]}
    return ctx

def _substitute(template: str, ctx: Dict[str, Any]) -> str:
    """
    Perform {{ var }} substitution on a template using the given context.
    """

    ctx = _apply_aliases(ctx)

    def replacer(match: re.Match) -> str:
        key = match.group(1)  # the name inside {{ ... }}
        if key not in ctx:
            # Leave placeholder intact if no value was provided
            return match.group(0)
        return _jsonify(ctx[key])

    return _VAR_RE.sub(replacer, template)

# --------------------------
# Public API
# --------------------------
def render_prompt(*args, **kwargs) -> str:
    """
    Flexible renderer supporting **both** calling styles:

    NEW STYLE (preferred):
        render_prompt(template_str, turns=..., snapshot=..., preview_only=..., locale=...)

    LEGACY STYLE (backward-compatible):
        render_prompt(system_content, task_content, context_dict)

    Behavior:
      - If called with a single positional argument (template) and keyword args,
        substitute {{...}} placeholders from kwargs (and optional 'context' kw).
      - If called with three positional args, treat as (system, task, context_dict),
        combine system + separator + task, then substitute.
    """
    if len(args) == 1:
        # New style: render_prompt(template, **kwargs)
        template = args[0]
        # Allow an optional explicit 'context' dict + extra kwargs
        ctx = {}
        if "context" in kwargs and isinstance(kwargs["context"], dict):
            ctx.update(kwargs["context"])
        # Merge remaining kwargs into context
        for k, v in kwargs.items():
            if k == "context":
                continue
            ctx[k] = v
        return _substitute(template, ctx)

    elif len(args) == 3:
        # Legacy style: render_prompt(system_content, task_content, context_dict)
        system_content, task_content, context = args
        if not isinstance(context, dict):
            raise TypeError("Legacy render_prompt expects a dict as the third argument.")
        composed = system_content.rstrip() + "\n\n---\n\n" + task_content.lstrip()
        return _substitute(composed, context)

    else:
        raise TypeError(
            "render_prompt expected either (template, **kwargs) or (system_content, task_content, context_dict)."
        )
