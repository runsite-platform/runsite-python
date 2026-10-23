import pytest


@pytest.fixture(autouse=True)
def clean_environment() -> None:
    """Live tests read the real RUNSITE_*, EMAIL_* and S3_* variables."""


@pytest.fixture(autouse=True)
def fake_clock() -> None:
    """Live tests really wait between retries."""
