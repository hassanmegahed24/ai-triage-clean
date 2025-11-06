# app/routes/reasoning.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any, Dict
from app.clients.reasoning_client import ReasoningClient
from app.utils.prompt_loader import load_system_prompt, load_task_prompt, render_prompt
from app.services.snapshot_builder import build_snapshot

router = APIRouter()
client = ReasoningClient()
class PreviewIn(BaseModel):
    task: str
    transcript: str
    ehr_json: Dict[str, Any] = {}
    language: str = "en"

@router.post("/preview")
def preview_reasoning(body: PreviewIn): #return final structured prompt first. 
    """
    Endpoint to preview the composed prompt for a given task and context.
    This helps in debugging and understanding what is sent to the LLM.
    """
    try:
        system = load_system_prompt()
        task = load_task_prompt(body.task)
        
        context = {
            "transcript": body.transcript,
            "ehr_json": body.ehr_json,
            "language": body.language
        }

        prompt = render_prompt(system, task, context)
        return {"composed_prompt": prompt}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.get("/reasoning")
def get_reasoning():
    """
    Simple health-check endpoint.
    """
    return {"message": "Reasoning route is live!"}


# app/routes/reasoning.py





class PromptTestIn(BaseModel):
    patient_id: int
    transcript: str
    task: str = "soap"   # default task to check substitution
@router.post("/test-placeholders")
async def test_placeholders(body: PromptTestIn):
    """
    Test that placeholders {{transcript}} and {{ehr_json}}/{{snapshot}}
    are replaced correctly when pulling a specific patient's EHR.
    """
    try:
        # 1. Fetch snapshot for this patient
        snapshot = await build_snapshot(body.patient_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"EHR fetch failed: {e}")

    try:
        # 2. Load system + task prompt
        system = load_system_prompt()
        task = load_task_prompt(body.task)

        # 3. Render prompt with transcript + snapshot
        prompt = render_prompt(
            system,
            task,
            context={
                "transcript": body.transcript,
                "ehr_json": snapshot
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Prompt error: {e}")

    return {
        "patient_id": body.patient_id,
        "final_prompt": prompt
    }

class RunReasoningIn(BaseModel):
    patient_id: int
    transcript: str
    language: str 

@router.post("/run")
async def run_reasoning(body: RunReasoningIn):
    """
    Run a specific reasoning task with transcript + patient snapshot.
    """

    try:
        snapshot = await build_snapshot(body.patient_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"EHR snapshot fetch failed: {e}")

   

    try:
        result = client.generate_reasoning(
            body.transcript,
           snapshot,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))