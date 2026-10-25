import subprocess
import sys

import runsite


def test_default_instances() -> None:
    assert isinstance(runsite.email, runsite.Email)
    assert isinstance(runsite.secrets, runsite.Secrets)
    assert isinstance(runsite.secrets, runsite.SecretReader)
    assert isinstance(runsite.storage, runsite.Storage)
    assert runsite.email.webhooks.verify is runsite.verify_webhook


def test_import_needs_no_configuration() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import runsite; print(runsite.secrets.is_runsite())"],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": ""},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_import_does_not_load_boto3() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import sys, runsite; print('boto3' in sys.modules)"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.stdout.strip() == "False"


def test_webhook_events_constant() -> None:
    assert isinstance(runsite.WEBHOOK_EVENTS, frozenset)
    assert "delivered" in runsite.WEBHOOK_EVENTS


EXPECTED_PUBLIC_NAMES = {
    "WEBHOOK_EVENTS",
    "APIConnectionError",
    "APIStatusError",
    "APITimeoutError",
    "AccessKey",
    "AccessKeyInfo",
    "ApplyResult",
    "AsyncBucket",
    "AsyncEmail",
    "AsyncObjectStream",
    "AsyncSecrets",
    "AsyncStorage",
    "AuthenticationError",
    "BadRequestError",
    "Bucket",
    "BucketInfo",
    "BulkResult",
    "ConfigurationError",
    "ConflictError",
    "Email",
    "EmailQuotaExceededError",
    "EmailRecord",
    "EnvVariable",
    "InternalServerError",
    "InvalidSecretError",
    "KeyPermissions",
    "MissingSecretError",
    "NotFoundError",
    "ObjectInfo",
    "ObjectNotFoundError",
    "PermissionDeniedError",
    "RateLimitError",
    "RunsiteError",
    "Secret",
    "SecretReader",
    "Secrets",
    "SentEmail",
    "ServiceUnavailableError",
    "Storage",
    "StorageAccessDeniedError",
    "StorageError",
    "UnprocessableEntityError",
    "UpstreamError",
    "WebhookEvent",
    "WebhookVerificationError",
    "__version__",
    "email",
    "secrets",
    "storage",
    "verify_webhook",
}


def test_public_names() -> None:
    assert set(runsite.__all__) == EXPECTED_PUBLIC_NAMES


def test_every_export_resolves() -> None:
    for name in runsite.__all__:
        assert hasattr(runsite, name), name


def test_errors_share_a_base() -> None:
    for name in runsite.__all__:
        exported = getattr(runsite, name)
        if isinstance(exported, type) and issubclass(exported, Exception):
            assert issubclass(exported, runsite.RunsiteError), name
