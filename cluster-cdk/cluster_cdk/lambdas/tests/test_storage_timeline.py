import os
import datetime

import boto3
import pytest
from unittest.mock import MagicMock, patch

from moto import mock_aws
from volume_management import lambda_handler

AWS_REGION_NAME = "us-west-2"

DATE_FORMAT = "%Y-%m-%d %H:%M:%S%z"
NOW = datetime.datetime.strptime("2000-01-01 00:00:00+0000", DATE_FORMAT)
YESTERDAY = NOW - datetime.timedelta(hours=24)
TOMORROW = NOW + datetime.timedelta(hours=24)
NEXT_WEEK = NOW + datetime.timedelta(weeks=1)


class MockDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def mock_set_sso_token() -> str:
    return "mock"


def mock_send_email_to_portal(email_payload: dict) -> None:
    print(f"{email_payload}=")


@pytest.fixture
def mock_get_eks_client():
    """Fixture to mock kubernetes.client.CoreV1Api and load_kube_config."""
    # Prevent the test from trying to load an actual local kubeconfig file
    with patch("volume_management.k8s_config.load_kube_config"):
        # Patch CoreV1Api where it is imported/used in your application module
        with patch("volume_management.k8s_client.CoreV1Api") as mock_core_v1_class:
            # mock_core_v1_class() represents the instantiated 'v1' object
            mock_api_instance = mock_core_v1_class.return_value
            yield mock_api_instance


@pytest.fixture
def setup_mock_aws_credentials():
    """Mocked AWS Credentials for moto. Just to make sure nothing unfortunate happens."""
    os.environ["AWS_ACCESS_KEY_ID"] = "mock"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "mock"
    os.environ["AWS_DEFAULT_REGION"] = AWS_REGION_NAME


@pytest.fixture
def setup_mock_volumes(setup_mock_aws_credentials):
    """Context manager to provision volumes with various tags."""
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=AWS_REGION_NAME)

        volume_configs = [
            {
                "name": "new_volume",
                "tags": [
                    {"Key": "volume-delete-time", "Value": f"{TOMORROW}"},
                ],
            },
            {
                "name": "expired_volume",
                "tags": [
                    {"Key": "volume-delete-time", "Value": f"{YESTERDAY}"},
                ],
            },
            {
                "name": "protected_expired_volume",
                "tags": [
                    {"Key": "do-not-delete", "Value": "true"},
                    {"Key": "volume-delete-time", "Value": f"{YESTERDAY}"},
                ],
            },
        ]

        created_volumes = {}
        for config in volume_configs:
            v = ec2.create_volume(
                AvailabilityZone=f"{AWS_REGION_NAME}a",
                Size=10,
                TagSpecifications=[{"ResourceType": "volume", "Tags": config["tags"]}],
            )
            created_volumes[config["name"]] = v

        yield created_volumes


@pytest.fixture
def setup_mock_snapshots(setup_mock_aws_credentials, setup_mock_volumes):
    """Context manager to provision snapshots with various tags."""
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=AWS_REGION_NAME)

        snapshot_configs = [
            {
                "name": "new_snapshot",
                "tags": [
                    {"Key": "snapshot-delete-time", "Value": f"{NEXT_WEEK}"},
                ],
            },
            {
                "name": "older_snapshot",
                "tags": [
                    {"Key": "snapshot-delete-time", "Value": f"{TOMORROW}"},
                ],
            },
            {
                "name": "expired_snapshot",
                "tags": [
                    {"Key": "snapshot-delete-time", "Value": f"{YESTERDAY}"},
                ],
            },
        ]

        created_snapshots = {}
        for config in snapshot_configs:
            s = ec2.create_snapshot(
                VolumeId=setup_mock_volumes["new_volume"]["VolumeId"],
                TagSpecifications=[
                    {"ResourceType": "snapshot", "Tags": config["tags"]}
                ],
            )
            created_snapshots[config["name"]] = s
        yield created_snapshots


def test_new_volume_no_shapshot(
    setup_mock_volumes, setup_mock_snapshots, mock_get_eks_client, monkeypatch
):
    """New volume created with no snapshot"""
    # Mock internal variables using monkeypatch
    monkeypatch.setattr("volume_management.datetime.datetime", MockDatetime)
    monkeypatch.setattr("volume_management.set_sso_secret", mock_set_sso_token)
    monkeypatch.setattr(
        "volume_management.send_email_to_portal", mock_send_email_to_portal
    )
    monkeypatch.setattr("volume_management.get_eks_client", mock_get_eks_client)

    monkeypatch.setenv("LAB_SHORT_NAME", "mock")
    monkeypatch.setenv("CLUSTER_NAME", "mock")
    monkeypatch.setenv("SNAPSHOT_WARNING_DAYS", "1")
    monkeypatch.setenv("SNAPSHOT_GRACEPERIOD_DAYS", "1")
    monkeypatch.setenv("ALERT_SNS_TOPIC_ARN", "")
    monkeypatch.setenv("PORTAL_DOMAINS", "mock.cloudfront.net")

    # 2. Run script with an empty event parameter
    result = lambda_handler({}, None)
    print(result)

    # 3. Assertions
    # assert result["statusCode"] == 200
    # assert setup_mock_volumes["prod_alpha"] in processed
    # assert setup_mock_volumes["staging_alpha"] not in processed
