from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from .conventions import VocabRef, validate_iso8601_timestamp


class Trail(BaseModel):
    """An exploratory lead — a noticed adjacency between two ideas/clusters.

    The divergent arrow of the consolidation loop: a bridge between
    themes that keep landing near each other. Inert until pulled (demand-gated);
    a pull spawns a bounded, focused look that may yield a memory, project, or new
    trails. See the consolidation design notes.
    """

    id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)

    connects: list[str] = Field(default_factory=list)  # the 2+ ideas/clusters it bridges
    signal: str = Field(..., min_length=1)             # why they seem adjacent (observed evidence)
    question: str = Field(..., min_length=1)           # the open thread to pull

    status: VocabRef  # vocab:trail_statuses:{open|pulled|led-somewhere|dead}
    score: float = Field(default=0.0, ge=0.0, le=1.0)  # novelty × recurrence × surprise

    # Shared-key Layer 3: pointers back to the Substrate traces behind the adjacency.
    provenance: list[str] = Field(default_factory=list)

    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_trail(self) -> "Trail":
        self.created_at = validate_iso8601_timestamp(self.created_at)
        self.updated_at = validate_iso8601_timestamp(self.updated_at)
        if self.first_seen is not None:
            self.first_seen = validate_iso8601_timestamp(self.first_seen)
        if self.last_seen is not None:
            self.last_seen = validate_iso8601_timestamp(self.last_seen)
        return self
