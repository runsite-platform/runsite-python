import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from runsite._secrets.models import ApplyResult, BulkResult, EnvVariable
from runsite._storage.models import AccessKey, AccessKeyInfo, BucketInfo, KeyPermissions

SCHEMA: dict[str, Any] = json.loads((Path(__file__).parent / "fixtures" / "openapi-public.json").read_text())
COMPONENTS: dict[str, Any] = SCHEMA["components"]["schemas"]
APPLY_ENV_PATH = "/api/v1/web-services/{web_service_id}/apply-env"

MODEL_TO_SCHEMA: list[tuple[type[BaseModel], str]] = [
    (EnvVariable, "EnvVarResponse"),
    (BulkResult, "EnvVarBulkResponse"),
    (ApplyResult, "DeploymentStatus"),
    (BucketInfo, "BucketResponse"),
    (KeyPermissions, "KeyPermissions"),
    (AccessKeyInfo, "ApiKeyInfo"),
    (AccessKey, "CreateKeyResponse"),
]

LIST_FIELDS: list[tuple[str, str]] = [
    ("EnvVarListResponse", "environment_variables"),
    ("PublicBucketListResponse", "buckets"),
    ("PublicStorageKeyListResponse", "keys"),
]

USED_OPERATIONS: list[tuple[str, str]] = [
    ("/api/v1/storage/buckets", "get"),
    ("/api/v1/storage/buckets", "post"),
    ("/api/v1/storage/buckets/{name}", "get"),
    ("/api/v1/storage/buckets/{name}", "patch"),
    ("/api/v1/storage/buckets/{name}", "delete"),
    ("/api/v1/storage/keys", "get"),
    ("/api/v1/storage/keys", "post"),
    ("/api/v1/storage/keys/{key_id}", "delete"),
    ("/api/v1/web-services/{web_service_id}/env", "get"),
    ("/api/v1/web-services/{web_service_id}/env/bulk", "put"),
    (APPLY_ENV_PATH, "post"),
]


def component(name: str) -> dict[str, Any]:
    """A component schema by name; FastAPI prefixes the module path when two models share one name."""
    if name in COMPONENTS:
        found: dict[str, Any] = COMPONENTS[name]
        return found
    matches = [schema for key, schema in COMPONENTS.items() if key.endswith(f"__{name}")]
    assert len(matches) == 1, f"{name} is not in the public schema exactly once"
    single_match: dict[str, Any] = matches[0]
    return single_match


@pytest.mark.parametrize(("model", "schema_name"), MODEL_TO_SCHEMA, ids=[name for _, name in MODEL_TO_SCHEMA])
def test_model_fields_exist_in_schema(model: type[BaseModel], schema_name: str) -> None:
    properties = component(schema_name)["properties"]
    field_names = [field.alias or name for name, field in model.model_fields.items()]
    missing = [field_name for field_name in field_names if field_name not in properties]
    assert missing == []


@pytest.mark.parametrize(("schema_name", "field_name"), LIST_FIELDS)
def test_list_responses_wrap_items_in_the_expected_field(schema_name: str, field_name: str) -> None:
    assert field_name in component(schema_name)["properties"]


@pytest.mark.parametrize(
    ("path", "method"), USED_OPERATIONS, ids=[f"{method} {path}" for path, method in USED_OPERATIONS]
)
def test_used_operations_exist(path: str, method: str) -> None:
    assert path in SCHEMA["paths"]
    assert method in SCHEMA["paths"][path]


def test_apply_env_returns_deployment_status() -> None:
    responses = json.dumps(SCHEMA["paths"][APPLY_ENV_PATH]["post"]["responses"])
    assert "DeploymentStatus" in responses
