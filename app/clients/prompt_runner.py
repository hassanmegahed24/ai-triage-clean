"""
Minimal prompt runner utilities used by realtime modules.

Currently provides `render_system_instruction(system_file)` which loads a
system prompt text from `app/prompts/<system_file>` using the shared
prompt loader.
"""

from app.utils.prompt_loader import load_prompt


def render_system_instruction(system_file: str = "system_global.txt") -> str:
    """Return the contents of the given system prompt file.

    Falls back to raising FileNotFoundError if the file does not exist so
    callers can handle the error appropriately.
    """
    return load_prompt(system_file)

