import os

import boto3
import pytest

from moto import mock_aws
from volume_management import lambda_handler


@pytest.fixture
def aws_credentials():
    """Mocked AWS Credentials for moto."""
    os.environ["AWS_ACCESS_KEY_ID"] = "mock"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "mock"
    os.environ["AWS_DEFAULT_REGION"] = "us-west-2"


@pytest.fixture
def setup_mock_ec2(aws_credentials):
    """Context manager to provision volumes with various tags."""
    with mock_aws():
        ec2 = boto3.client("ec2", region_name="us-west-2")

        volume_configs = [
            {
                "name": "prod_alpha",
                "tags": [
                    {"Key": "Environment", "Value": "Production"},
                    {"Key": "Project", "Value": "Alpha"},
                ],
            },
            {
                "name": "staging_alpha",
                "tags": [
                    {"Key": "Environment", "Value": "Staging"},
                    {"Key": "Project", "Value": "Alpha"},
                ],
            },
        ]

        created_volumes = {}
        for config in volume_configs:
            v = ec2.create_volume(
                AvailabilityZone="us-east-1a",
                Size=10,
                TagSpecifications=[{"ResourceType": "volume", "Tags": config["tags"]}],
            )
            created_volumes[config["name"]] = v["VolumeId"]

        yield created_volumes


def test_finds_production_volumes_via_env(setup_mock_ec2, monkeypatch):
    """Inject environment variables to filter by Environment=Production."""
    # 1. Mock the environment variables using monkeypatch
    monkeypatch.setenv("TARGET_TAG_KEY", "Environment")
    monkeypatch.setenv("TARGET_TAG_VALUE", "Production")

    # 2. Run script with an empty event parameter
    result = lambda_handler({}, None)
    processed = result["processed_volumes"]

    # 3. Assertions
    assert result["statusCode"] == 200
    assert setup_mock_ec2["prod_alpha"] in processed
    assert setup_mock_ec2["staging_alpha"] not in processed
