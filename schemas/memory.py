from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from .conventions import VocabRef, validate_iso8601_timestamp

_MEMORY_STATUSES = {"active", "superseded"}


class Memory(BaseModel):
    """A consolidated memory promoted from the Substrate episodic floor into Atlas.

    Produced by the consolidation loop: a stabilized, recurring
    episodic pattern synthesized into one durable statement and projected into a
    functional type. Reversible — a contradicting later memory supersedes it
    (status='superseded' + superseded_by) rather than deleting it.
    See the consolidation design notes.
    """

    id: str = Field(..., min_length=1)
    memory_type: VocabRef  # vocab:memory_types:{identity|preference|expertise|decision|reference}
    statement: str = Field(..., min_length=1)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    status: str = Field(default="active")  # active | superseded
    superseded_by: Optional[str] = None

    # Shared-key Layer 3: pointers back to the Substrate traces that justify this memory
    # (episode_id, session_id:turn_index, or file_path#section).
    provenance: list[str] = Field(default_factory=list)
    recurrence_sessions: int = Field(default=0, ge=0)

    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_memory(self) -> "Memory":
        self.created_at = validate_iso8601_timestamp(self.created_at)
        self.updated_at = validate_iso8601_timestamp(self.updated_at)
        if self.first_seen is not None:
            self.first_seen = validate_iso8601_timestamp(self.first_seen)
        if self.last_seen is not None:
            self.last_seen = validate_iso8601_timestamp(self.last_seen)

        if self.status not in _MEMORY_STATUSES:
            raise ValueError(f"status must be one of: {sorted(_MEMORY_STATUSES)}")
        if self.status == "superseded" and not self.superseded_by:
            raise ValueError("superseded memories must set superseded_by")
        if self.status != "superseded" and self.superseded_by:
            raise ValueError("superseded_by can only be set when status='superseded'")

        return self
