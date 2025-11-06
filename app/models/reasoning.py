# app/models/reasoning.py

from pydantic import BaseModel
from typing import List, Optional

class SOAP(BaseModel):
    subjective: str
    objective: str
    assessment: List[str]
    plan: List[str]

class ReasoningResponse(BaseModel):
    questions: List[str]
    reasoning_summary: str
    soap: SOAP
    next_steps: List[str]
    speech_output: str
