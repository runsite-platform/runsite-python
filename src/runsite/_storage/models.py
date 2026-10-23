from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from runsite._secrets.reader import Secret


class BucketInfo(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    is_public: bool
    created_at: datetime
    storage_bytes_used: int = 0
    object_count: int = 0


class KeyPermissions(BaseModel):
    model_config = ConfigDict(extra="allow")

    read: bool
    write: bool
    delete: bool


class _KeyBase(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    access_key: str
    name: str | None = None
    permissions: KeyPermissions
    bucket_id: str | None = None


class AccessKeyInfo(_KeyBase):
    """An existing S3 access key as listed by the API; the secret is never returned again."""

    is_active: bool = True
    created_at: datetime | None = None


class AccessKey(_KeyBase):
    """A newly created key. `secret_key` is shown only once; store it now."""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    secret_key: Secret
    endpoint_url: str
    region: str

    @field_validator("secret_key", mode="before")
    @classmethod
    def wrap_secret(cls, value: object) -> Secret:
        return value if isinstance(value, Secret) else Secret(str(value))


class ObjectInfo(BaseModel):
    model_config = ConfigDict(extra="allow")

    key: str
    size: int
    etag: str | None = None
    last_modified: datetime | None = None
