import os

import pytest

from runsite._config import (
    Environment,
    email_settings,
    platform_settings,
    resolve_bucket_name,
    resolve_service_id,
    s3_credentials,
)
from runsite._errors import ConfigurationError, api_error_from_response, email_error_from_response
from tests.constants import API_KEY

RUNSITE_S3 = {
    "S3_ACCESS_KEY_ID": "AKIA1",
    "S3_SECRET_ACCESS_KEY": "secret",
    "S3_ENDPOINT_URL": "https://s3.runsite.test",
}
AWS_S3 = {
    "AWS_ACCESS_KEY_ID": "AKIA2",
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
    "AWS_ENDPOINT_URL_S3": "https://s3.other.test",
}


def test_get_returns_first_non_empty() -> None:
    environment = Environment({"FIRST": "", "SECOND": "value"})

    assert environment.get("FIRST", "SECOND") == "value"
    assert environment.get("MISSING") is None


def test_missing_lists_unset_and_empty_names_in_order() -> None:
    environment = Environment({"SET": "value", "EMPTY": ""})

    assert environment.missing("EMPTY", "SET", "ABSENT") == ["EMPTY", "ABSENT"]
    assert environment.missing("SET") == []


def test_require_prefers_explicit() -> None:
    environment = Environment({"RUNSITE_API_KEY": "from-env"})

    assert environment.require("explicit", "RUNSITE_API_KEY", hint="") == "explicit"
    assert environment.require(None, "RUNSITE_API_KEY", hint="") == "from-env"


def test_require_names_the_variable() -> None:
    with pytest.raises(ConfigurationError, match=r"RUNSITE_API_KEY is not set\. Create one"):
        Environment({}).require(None, "RUNSITE_API_KEY", hint="Create one in the dashboard.")


def test_without_a_mapping_reads_the_process_environment_on_each_call(monkeypatch: pytest.MonkeyPatch) -> None:
    environment = Environment()
    monkeypatch.setenv("RUNSITE_BASE_URL", "https://late.runsite.test")

    assert environment.mapping() is os.environ
    assert environment.get("RUNSITE_BASE_URL") == "https://late.runsite.test"


def test_injected_mapping_ignores_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNSITE_API_KEY", API_KEY)

    assert Environment({}).get("RUNSITE_API_KEY") is None


def test_platform_settings_defaults() -> None:
    settings = platform_settings(
        Environment({"RUNSITE_API_KEY": API_KEY}), api_key=None, base_url=None, timeout=30.0, max_retries=2
    )

    assert settings.base_url == "https://api.runsite.app/api/v1"
    assert settings.headers == {"Authorization": f"Bearer {API_KEY}"}
    assert settings.error_factory is api_error_from_response


def test_platform_settings_base_url_from_environment() -> None:
    environment = Environment({"RUNSITE_BASE_URL": "https://staging.runsite.test/"})

    settings = platform_settings(environment, api_key=API_KEY, base_url=None, timeout=30.0, max_retries=2)

    assert settings.base_url == "https://staging.runsite.test/api/v1"


def test_platform_settings_rejects_non_api_key() -> None:
    with pytest.raises(ConfigurationError, match="ak_live_"):
        platform_settings(Environment({}), api_key="rs_eml_wrong", base_url=None, timeout=30.0, max_retries=2)


def test_platform_settings_names_the_missing_key() -> None:
    with pytest.raises(ConfigurationError, match="RUNSITE_API_KEY is not set"):
        platform_settings(Environment({}), api_key=None, base_url=None, timeout=30.0, max_retries=2)


def test_email_settings_from_environment() -> None:
    environment = Environment({"EMAIL_API_KEY": "rs_eml_1", "EMAIL_API_URL": "https://mail.runsite.test/emails"})

    settings = email_settings(environment, api_key=None, endpoint=None, timeout=60.0, max_retries=2)

    assert settings.base_url == "https://mail.runsite.test/emails"
    assert settings.headers == {"Api-Key": "rs_eml_1"}
    assert settings.error_factory is email_error_from_response


def test_service_id_prefers_the_first_given_candidate() -> None:
    environment = Environment({"RUNSITE_SERVICE_ID": "from-env"})

    assert resolve_service_id(environment, "per-call", "per-client") == "per-call"
    assert resolve_service_id(environment, None, "per-client") == "per-client"
    assert resolve_service_id(environment, None, None) == "from-env"
    with pytest.raises(ConfigurationError, match="RUNSITE_SERVICE_ID"):
        resolve_service_id(Environment({}), None, None)


def test_bucket_name_from_environment() -> None:
    assert resolve_bucket_name(Environment({"S3_BUCKET": "uploads"}), None) == "uploads"
    with pytest.raises(ConfigurationError, match="S3_BUCKET"):
        resolve_bucket_name(Environment({}), None)


def test_s3_credentials_from_runsite_variables() -> None:
    credentials = s3_credentials(Environment({**RUNSITE_S3, "S3_REGION": "eu-central"}))

    assert (credentials.key_id, credentials.secret, credentials.endpoint_url, credentials.region) == (
        "AKIA1",
        "secret",
        "https://s3.runsite.test",
        "eu-central",
    )
    assert "secret" not in repr(credentials)


def test_s3_credentials_fall_back_to_aws_variables_with_their_own_endpoint() -> None:
    credentials = s3_credentials(Environment(AWS_S3))

    assert credentials.key_id == "AKIA2"
    assert credentials.endpoint_url == "https://s3.other.test"
    assert credentials.region == "auto"


def test_runsite_variables_win_over_aws_variables() -> None:
    assert s3_credentials(Environment({**AWS_S3, **RUNSITE_S3})).key_id == "AKIA1"


def test_aws_keys_are_never_paired_with_runsite_endpoint() -> None:
    environment = Environment({**AWS_S3, "AWS_ENDPOINT_URL_S3": "", "S3_ENDPOINT_URL": "https://s3.runsite.test"})

    with pytest.raises(ConfigurationError, match="S3_ACCESS_KEY_ID"):
        s3_credentials(environment)
