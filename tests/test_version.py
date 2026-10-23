import runsite


def test_version_comes_from_package_metadata() -> None:
    assert runsite.__version__ == "0.1.0"
