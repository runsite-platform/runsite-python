import pytest

from runsite._config import read_env, resolve
from runsite._errors import ConfigurationError, api_error_from_response
from runsite._platform import platform_settings
from tests.constants import API_KEY


def test_read_env_returns_first_non_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRST", "")
    monkeypatch.setenv("SECOND", "value")

    assert read_env("FIRST", "SECOND") == "value"
    assert read_env("MISSING") is None


def test_resolve_prefers_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNSITE_API_KEY", "from-env")

    assert resolve("explicit", "RUNSITE_API_KEY", hint="") == "explicit"
    assert resolve(None, "RUNSITE_API_KEY", hint="") == "from-env"


def test_resolve_names_the_variable() -> None:
    with pytest.raises(ConfigurationError, match=r"RUNSITE_API_KEY is not set\. Create one"):
        resolve(None, "RUNSITE_API_KEY", hint="Create one in the dashboard.")


def test_platform_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNSITE_API_KEY", API_KEY)

    settings = platform_settings(api_key=None, base_url=None, timeout=30.0, max_retries=2)

    assert settings.base_url == "https://api.runsite.app/api/v1"
    assert settings.headers == {"Authorization": f"Bearer {API_KEY}"}
    assert settings.error_factory is api_error_from_response


def test_platform_settings_base_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNSITE_BASE_URL", "https://staging.runsite.test/")

    settings = platform_settings(api_key=API_KEY, base_url=None, timeout=30.0, max_retries=2)

    assert settings.base_url == "https://staging.runsite.test/api/v1"


def test_platform_settings_rejects_non_api_key() -> None:
    with pytest.raises(ConfigurationError, match="ak_live_"):
        platform_settings(api_key="rs_eml_wrong", base_url=None, timeout=30.0, max_retries=2)
