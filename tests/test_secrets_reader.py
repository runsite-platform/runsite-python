import pytest

from runsite._errors import InvalidSecretError, MissingSecretError
from runsite._secrets.reader import Secret, SecretReader


def reader_with(**variables: str) -> SecretReader:
    return SecretReader(environ=variables)


def test_default_source_is_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUNSITE_SDK_READER_PROBE", raising=False)
    reader = SecretReader()
    assert reader.get("RUNSITE_SDK_READER_PROBE", default=None) is None

    monkeypatch.setenv("RUNSITE_SDK_READER_PROBE", "one")
    assert reader.get("RUNSITE_SDK_READER_PROBE") == "one"
    monkeypatch.setenv("RUNSITE_SDK_READER_PROBE", "two")
    assert reader.get("RUNSITE_SDK_READER_PROBE") == "two"


def test_get_reads_the_mapping_on_every_call() -> None:
    environ = {"TOKEN": "one"}
    reader = SecretReader(environ=environ)
    assert reader.get("TOKEN") == "one"
    environ["TOKEN"] = "two"
    assert reader.get("TOKEN") == "two"


def test_get_missing_raises() -> None:
    with pytest.raises(MissingSecretError, match="TOKEN"):
        reader_with().get("TOKEN")


def test_get_missing_with_default() -> None:
    assert reader_with().get("TOKEN", default=None) is None
    assert reader_with().get("TOKEN", default="fallback") == "fallback"


def test_empty_string_counts_as_missing() -> None:
    reader = reader_with(TOKEN="", WORKERS="", APP_NAME="", APP_PORT="8000")

    with pytest.raises(MissingSecretError, match="TOKEN"):
        reader.get("TOKEN")
    assert reader.get("TOKEN", default="fallback") == "fallback"
    assert reader.get_int("WORKERS", default=4) == 4
    with pytest.raises(MissingSecretError) as caught:
        reader.require("TOKEN", "WORKERS")
    assert caught.value.keys == ["TOKEN", "WORKERS"]
    assert reader.is_runsite() is False


@pytest.mark.parametrize(("raw", "expected"), [("4", 4), (" 12 ", 12), ("-1", -1)])
def test_get_int(raw: str, expected: int) -> None:
    assert reader_with(WORKERS=raw).get_int("WORKERS") == expected


def test_get_int_invalid_hides_value() -> None:
    with pytest.raises(InvalidSecretError) as caught:
        reader_with(WORKERS="sk_live_secret").get_int("WORKERS")
    assert "WORKERS" in str(caught.value)
    assert "sk_live_secret" not in str(caught.value)


def test_get_int_default() -> None:
    assert reader_with().get_int("WORKERS", default=4) == 4


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_get_bool_true(raw: str) -> None:
    assert reader_with(DEBUG=raw).get_bool("DEBUG") is True


@pytest.mark.parametrize("raw", ["0", "false", "No", "off"])
def test_get_bool_false(raw: str) -> None:
    assert reader_with(DEBUG=raw).get_bool("DEBUG") is False


def test_get_bool_rejects_other_values() -> None:
    with pytest.raises(InvalidSecretError):
        reader_with(DEBUG="maybe").get_bool("DEBUG")


def test_get_float() -> None:
    assert reader_with(RATIO="0.25").get_float("RATIO") == 0.25


def test_get_list() -> None:
    reader = reader_with(HOSTS="a.com, b.com,,c.com ")
    assert reader.get_list("HOSTS") == ["a.com", "b.com", "c.com"]
    assert reader.get_list("OTHER_HOSTS", default=[]) == []


def test_get_json() -> None:
    assert reader_with(FLAGS='{"beta": true}').get_json("FLAGS") == {"beta": True}


def test_get_json_invalid() -> None:
    with pytest.raises(InvalidSecretError):
        reader_with(FLAGS="{nope").get_json("FLAGS")


def test_secret_is_masked() -> None:
    secret = reader_with(STRIPE_KEY="sk_live_123").secret("STRIPE_KEY")

    assert repr(secret) == "Secret('********')"
    assert str(secret) == "********"
    assert f"{secret}" == "********"
    assert secret.reveal() == "sk_live_123"
    assert secret == Secret("sk_live_123")
    assert secret != Secret("sk_live_456")


def test_secret_equality_handles_non_ascii_values() -> None:
    assert Secret("pässwörd-✓") == Secret("pässwörd-✓")
    assert Secret("pässwörd-✓") != Secret("password")
    assert Secret("pässwörd-✓") != "pässwörd-✓"


def test_require_lists_every_missing_key() -> None:
    with pytest.raises(MissingSecretError) as caught:
        reader_with(FIRST="1").require("FIRST", "SECOND", "THIRD")
    assert caught.value.keys == ["SECOND", "THIRD"]


def test_database_and_redis_urls() -> None:
    reader = reader_with(
        DATABASE_URL="postgresql://u:p@db:5432/app",
        DATABASE_URL_2="postgresql://u:p@db2:5432/app",
        REDIS_URL="redis://cache:6379",
    )

    assert reader.database_url() == "postgresql://u:p@db:5432/app"
    assert reader.database_url("DATABASE_URL_2") == "postgresql://u:p@db2:5432/app"
    assert reader.redis_url() == "redis://cache:6379"


def test_is_runsite() -> None:
    assert reader_with().is_runsite() is False
    assert reader_with(APP_NAME="api", APP_PORT="8000").is_runsite() is True
