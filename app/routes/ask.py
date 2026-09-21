from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/ask", tags=["Ask"])


class AskRequest(BaseModel):
    query: str
    history: list | None = None


class AskResponse(BaseModel):
    query: str
    answer: str


@router.post("/", response_model=AskResponse)
def ask(request: AskRequest):
    """
    Shell for the future ROAM agent loop (geospatial document intelligence
    and spatial validation). Tool-calling wiring lives in app/agent/; this
    endpoint intentionally does not invoke it yet.
    """
    return AskResponse(
        query=request.query,
        answer="ROAM — coming soon.",
    )
