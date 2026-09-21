"""AgentAction: a high-level action taken by the (future) ROAM agent."""

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class AgentActionType(str, Enum):
    GEOCODE = "geocode"
    SEARCH_NEARBY = "search_nearby"
    CALCULATE_ROUTE = "calculate_route"
    SEARCH_ALONG_ROUTE = "search_along_route"
    RETRIEVE_CONTEXT = "retrieve_context"
    RANK_RESULTS = "rank_results"


class AgentAction(BaseModel):
    """A single high-level action the agent performed (or plans to perform).

    Kept generic (`input`/`output` as free-form dicts) since concrete tool
    signatures don't exist yet — this schema just needs to record what
    happened, not enforce per-action shapes.
    """

    action_type: AgentActionType
    input: dict[str, Any] = Field(default_factory=dict, description="Parameters the action was invoked with.")
    output: Optional[Any] = Field(None, description="Result of the action, e.g. a list of POIs or a Route.")

    success: Optional[bool] = None
    error: Optional[str] = None
