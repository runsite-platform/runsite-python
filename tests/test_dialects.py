import pytest

from runsite import AsyncEmail, AsyncSecrets, AsyncStorage, Email, Secrets, Storage
from runsite._dialects import api_error_from_response, email_api, email_error_from_response, platform_api
from runsite._environment import Environment
from runsite._errors import ConfigurationError
from tests.constants import API_KEY

DEFAULT_LIMITS = {"timeout": 30.0, "max_retries": 2}
CLIENTS = [Email, AsyncEmail, Secrets, AsyncSecrets, Storage, AsyncStorage]


def test_platform_api_defaults() -> None:
    settings = platform_api(Environment({"RUNSITE_API_KEY": API_KEY}), api_key=None, base_url=None, **DEFAULT_LIMITS)()

    assert settings.base_url == "https://api.runsite.app/api/v1"
    assert settings.headers == {"Authorization": f"Bearer {API_KEY}"}
    assert settings.error_factory is api_error_from_response


def test_platform_api_base_url_from_environment() -> None:
    environment = Environment({"RUNSITE_BASE_URL": "https://staging.runsite.test/"})

    settings = platform_api(environment, api_key=API_KEY, base_url=None, **DEFAULT_LIMITS)()

    assert settings.base_url == "https://staging.runsite.test/api/v1"


def test_platform_api_rejects_non_api_key() -> None:
    provider = platform_api(Environment({}), api_key="rs_eml_wrong", base_url=None, **DEFAULT_LIMITS)

    with pytest.raises(ConfigurationError, match="ak_live_"):
        provider()


def test_platform_api_names_the_missing_key_only_when_used() -> None:
    provider = platform_api(Environment({}), api_key=None, base_url=None, **DEFAULT_LIMITS)

    with pytest.raises(ConfigurationError, match="RUNSITE_API_KEY is not set"):
        provider()


def test_email_api_from_environment() -> None:
    environment = Environment({"EMAIL_API_KEY": "rs_eml_1", "EMAIL_API_URL": "https://mail.runsite.test/emails"})

    settings = email_api(environment, api_key=None, endpoint=None, **DEFAULT_LIMITS)()

    assert settings.base_url == "https://mail.runsite.test/emails"
    assert settings.headers == {"Api-Key": "rs_eml_1"}
    assert settings.error_factory is email_error_from_response


@pytest.mark.parametrize("client_class", CLIENTS, ids=lambda cls: cls.__name__)
@pytest.mark.parametrize(("timeout", "max_retries"), [(0.0, 1), (float("nan"), 1), (5.0, -1)])
def test_clients_reject_invalid_limits_in_the_constructor(client_class: type, timeout: float, max_retries: int) -> None:
    with pytest.raises(ValueError):
        client_class(timeout=timeout, max_retries=max_retries, environ={})
