from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

RESTARTING_STATUS = "restarting"


class EnvVariable(BaseModel):
    """An environment variable of a web service; the API never returns its value."""

    model_config = ConfigDict(extra="ignore")

    id: str
    key: str
    is_secret: bool
    web_service_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class BulkResult(BaseModel):
    """How many variables one write created, updated and deleted."""

    model_config = ConfigDict(extra="ignore")

    created: int = 0
    updated: int = 0
    deleted: int = 0


class ApplyResult(BaseModel):
    """The web service status returned by `apply()`.

    `status` is `restarting` when a rolling restart with the new variables was queued. Any other status
    (for example `stopped`, `sleeping` or `failed`) means the service is not running: the variables are saved
    and take effect the next time it starts. `message` is a human-readable description of the outcome.
    """

    model_config = ConfigDict(extra="ignore")

    status: str
    container_id: str | None = None
    host_port: int | None = None
    message: str | None = None

    @property
    def restarting(self) -> bool:
        """True when a restart that applies the variables was queued."""
        return self.status == RESTARTING_STATUS
