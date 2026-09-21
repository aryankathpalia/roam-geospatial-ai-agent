"""Query: the user's request, plus whatever structure we extract from it."""

from typing import Any, Optional

from pydantic import BaseModel, Field


class Query(BaseModel):
    """A user's natural-language request, with room for extracted structure.

    `raw_query` is always required and authoritative. `entities` and
    `constraints` are populated later (e.g. by an NLU/agent step) and stay
    optional/empty until that exists.
    """

    raw_query: str = Field(..., description="The user's original natural-language request, verbatim.")
    entities: dict[str, Any] = Field(
        default_factory=dict, description="Extracted entities, e.g. {'location': 'Paris', 'category': 'coffee'}."
    )
    constraints: dict[str, Any] = Field(
        default_factory=dict, description="Extracted constraints, e.g. {'max_distance_km': 5, 'open_now': True}."
    )

    session_id: Optional[str] = Field(None, description="Conversation/session identifier, if applicable.")
