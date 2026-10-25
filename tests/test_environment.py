import os

import pytest

from runsite._environment import Environment
from runsite._errors import ConfigurationError
from tests.constants import API_KEY


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
