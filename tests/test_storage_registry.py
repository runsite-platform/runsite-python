import pytest

from runsite._environment import Environment
from runsite._errors import ConfigurationError
from runsite._storage.registry import resolve_bucket_name, s3_credentials

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
