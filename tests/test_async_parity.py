"""Every public sync method has an async twin with the same parameters, kinds and defaults."""

import inspect

import pytest

from runsite import AsyncBucket, AsyncEmail, AsyncSecrets, AsyncStorage, Bucket, Email, Secrets, Storage

TWINS = [(Email, AsyncEmail), (Secrets, AsyncSecrets), (Storage, AsyncStorage), (Bucket, AsyncBucket)]
RENAMED = {"close": "aclose"}


def public_methods(cls: type) -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(member)
        for name, member in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    }


def parameters(signature: inspect.Signature) -> list[tuple[str, inspect._ParameterKind, object, object]]:
    return [
        (parameter.name, parameter.kind, parameter.default, parameter.annotation)
        for parameter in signature.parameters.values()
    ]


@pytest.mark.parametrize(("sync_class", "async_class"), TWINS, ids=lambda cls: cls.__name__)
def test_async_twin_matches_sync_signatures(sync_class: type, async_class: type) -> None:
    sync_methods = public_methods(sync_class)
    async_methods = public_methods(async_class)

    for name, sync_signature in sync_methods.items():
        async_name = RENAMED.get(name, name)
        assert async_name in async_methods, f"{async_class.__name__} has no {async_name}"
        assert parameters(async_methods[async_name]) == parameters(sync_signature), f"{name} differs"


@pytest.mark.parametrize(("sync_class", "async_class"), TWINS[:3], ids=lambda cls: cls.__name__)
def test_async_twin_constructor_matches(sync_class: type, async_class: type) -> None:
    sync_parameters = parameters(inspect.signature(sync_class))
    async_parameters = parameters(inspect.signature(async_class))

    assert [entry for entry in async_parameters if entry[0] != "http_client"] == [
        entry for entry in sync_parameters if entry[0] != "http_client"
    ]
