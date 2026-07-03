from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator

from .conventions import validate_iso8601_timestamp

_SESSION_STATUSES = {"completed", "aborted", "confirm-pending", "error"}
_SESSION_LIFECYCLES = {"active", "archived", "pruned"}


class Session(BaseModel):
    """Canonical operator session entity for mobile and chat control surfaces."""

    id: str = Field(..., min_length=1)
    source: str = Field(default="telegram-bot", min_length=1)
    user_id: int

    timestamp: datetime
    status: str = Field(default="completed")

    transcript: Optional[str] = None
    summary: Optional[str] = None

    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    entities_touched: list[str] = Field(default_factory=list)
    project_ids: list[str] = Field(default_factory=list)

    source_request_id: Optional[str] = None
    episode_id: Optional[str] = None
    lifecycle: str = Field(default="active")
    retention_days: int = Field(default=30, ge=1, le=3650)
    archived_at: Optional[datetime] = None
    pruned_at: Optional[datetime] = None
    transcript_sha256: Optional[str] = None
    prune_reason: Optional[str] = None
    notes: Optional[str] = None

    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_session(self) -> "Session":
        self.timestamp = validate_iso8601_timestamp(self.timestamp)
        self.created_at = validate_iso8601_timestamp(self.created_at)
        self.updated_at = validate_iso8601_timestamp(self.updated_at)
        if self.archived_at is not None:
            self.archived_at = validate_iso8601_timestamp(self.archived_at)
        if self.pruned_at is not None:
            self.pruned_at = validate_iso8601_timestamp(self.pruned_at)

        if self.status not in _SESSION_STATUSES:
            raise ValueError(f"status must be one of: {sorted(_SESSION_STATUSES)}")
        if self.lifecycle not in _SESSION_LIFECYCLES:
            raise ValueError(f"lifecycle must be one of: {sorted(_SESSION_LIFECYCLES)}")

        if self.lifecycle == "active":
            if self.archived_at is not None or self.pruned_at is not None:
                raise ValueError("active sessions cannot include archived_at or pruned_at")
        elif self.lifecycle == "archived":
            if self.archived_at is None:
                raise ValueError("archived sessions must include archived_at")
            if self.pruned_at is not None:
                raise ValueError("archived sessions cannot include pruned_at")
        elif self.lifecycle == "pruned":
            if self.pruned_at is None:
                raise ValueError("pruned sessions must include pruned_at")
            if self.transcript:
                raise ValueError("pruned sessions cannot retain transcript")
            if not self.summary:
                raise ValueError("pruned sessions must preserve summary metadata")
            if not self.tool_calls:
                raise ValueError("pruned sessions must preserve tool_calls metadata")
            if not self.entities_touched:
                raise ValueError("pruned sessions must preserve entities_touched metadata")
            if not self.transcript_sha256:
                raise ValueError("pruned sessions must include transcript_sha256")

        if self.project_ids:
            deduped = list(dict.fromkeys(pid.strip() for pid in self.project_ids if pid and pid.strip()))
            self.project_ids = deduped

        if self.entities_touched:
            self.entities_touched = [item for item in self.entities_touched if item and str(item).strip()]

        return self
