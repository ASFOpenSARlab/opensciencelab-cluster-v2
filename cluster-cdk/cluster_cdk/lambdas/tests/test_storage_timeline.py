import importlib
import datetime

import boto3
import pytest
from unittest.mock import patch, MagicMock
from moto import mock_aws

import volume_management

AWS_REGION_NAME = "us-west-2"

DATE_FORMAT = "%Y-%m-%d %H:%M:%S%z"
NOW = datetime.datetime.strptime("2000-01-01 00:00:00+0000", DATE_FORMAT)
YESTERDAY = NOW - datetime.timedelta(hours=24)
TOMORROW = NOW + datetime.timedelta(hours=24)
NEXT_WEEK = NOW + datetime.timedelta(weeks=1)


@pytest.fixture
def setup_mock_secret_manager():
    """Setup a secrets manager secret to manage secrets"""
    with mock_aws():
        secret_manager = boto3.client("secretsmanager", region_name=AWS_REGION_NAME)

        response = secret_manager.create_secret(
            Name="mock-sso-secret",
            SecretString="xY0AoI3Bu61kwXZWGpegNxF_A00YOsE-kLqHVSgsdvQ=",
        )

        yield response["ARN"]


@pytest.fixture
def setup_mock_portal_post():
    with patch("requests.post") as mock_post:
        # Configure the default mock response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"message": "Success!"}
        mock_post.return_value = mock_response

        # Yield the mock object so the test can inspect or modify it
        yield mock_post


@pytest.fixture
def patched_volume_management(
    setup_mock_secret_manager, setup_mock_portal_post, monkeypatch
):

    class MockDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    def mock_get_eks_api():
        """Fixture to mock kubernetes.client.CoreV1Api and load_kube_config."""
        # Prevent the test from trying to load an actual local kubeconfig file
        # Patch CoreV1Api where it is imported/used in your application module
        with (
            patch("volume_management.k8s_config.load_kube_config"),
            patch("volume_management.k8s_client.CoreV1Api") as mock_core_v1_class,
        ):
            # mock_core_v1_class() represents the instantiated 'v1' object
            mock_instance = MagicMock()
            mock_core_v1_class.return_value = mock_instance

            yield mock_instance

    # Mock internal functions using monkeypatch
    monkeypatch.setattr("volume_management.datetime.datetime", MockDatetime)
    monkeypatch.setattr("volume_management.get_eks_api", mock_get_eks_api)

    # Mock default parameters
    monkeypatch.setenv("LAB_SHORT_NAME", "mocklab")
    monkeypatch.setenv("CLUSTER_NAME", "mock")
    monkeypatch.setenv("SSO_SECRET_ARN", setup_mock_secret_manager)
    monkeypatch.setenv("ALERT_SNS_TOPIC_ARN", "")
    monkeypatch.setenv("PORTAL_DOMAINS", "mock.cloudfront.net")


@pytest.fixture
def setup_mock_volumes():
    """Context manager to provision volumes with various tags."""
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=AWS_REGION_NAME)

        volume_configs = [
            {
                "name": "new_volume",
                "tags": [
                    {"Key": "volume-delete-time", "Value": f"{TOMORROW}"},
                    {
                        "Key": f"tag:{volume_management.CLUSTER_TAG}",
                        "Value": "mocklab",
                    },
                    {
                        "Key": f"tag:{volume_management.CLAIM_TAG}",
                        "Value": "mockuser",
                    },
                ],
            },
            {
                "name": "expired_volume",
                "tags": [
                    {"Key": "volume-delete-time", "Value": f"{YESTERDAY}"},
                    {
                        "Key": f"tag:{volume_management.CLUSTER_TAG}",
                        "Value": "mocklab",
                    },
                    {
                        "Key": f"tag:{volume_management.CLAIM_TAG}",
                        "Value": "mockuser",
                    },
                ],
            },
            {
                "name": "protected_expired_volume",
                "tags": [
                    {"Key": "do-not-delete", "Value": "true"},
                    {"Key": "volume-delete-time", "Value": f"{YESTERDAY}"},
                    {
                        "Key": f"tag:{volume_management.CLUSTER_TAG}",
                        "Value": "mocklab",
                    },
                    {
                        "Key": f"tag:{volume_management.CLAIM_TAG}",
                        "Value": "mockuser",
                    },
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
def setup_mock_snapshots(setup_mock_volumes):
    """Context manager to provision snapshots with various tags."""
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=AWS_REGION_NAME)

        snapshot_configs = [
            {
                "name": "new_snapshot",
                "tags": [
                    {"Key": "snapshot-delete-time", "Value": f"{NEXT_WEEK}"},
                    {
                        "Key": f"tag:{volume_management.CLUSTER_TAG}",
                        "Value": "mocklab",
                    },
                    {
                        "Key": f"tag:{volume_management.CLAIM_TAG}",
                        "Value": "mockuser",
                    },
                ],
            },
            {
                "name": "older_snapshot",
                "tags": [
                    {"Key": "snapshot-delete-time", "Value": f"{TOMORROW}"},
                    {
                        "Key": f"tag:{volume_management.CLUSTER_TAG}",
                        "Value": "mocklab",
                    },
                    {
                        "Key": f"tag:{volume_management.CLAIM_TAG}",
                        "Value": "mockuser",
                    },
                ],
            },
            {
                "name": "expired_snapshot",
                "tags": [
                    {"Key": "snapshot-delete-time", "Value": f"{YESTERDAY}"},
                    {
                        "Key": f"tag:{volume_management.CLUSTER_TAG}",
                        "Value": "mocklab",
                    },
                    {
                        "Key": f"tag:{volume_management.CLAIM_TAG}",
                        "Value": "mockuser",
                    },
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
    patched_volume_management,
    setup_mock_volumes,
    setup_mock_snapshots,
    monkeypatch,
):
    """New volume created with no snapshot"""
    monkeypatch.setenv("SNAPSHOT_WARNING_DAYS", "1")
    monkeypatch.setenv("SNAPSHOT_GRACEPERIOD_DAYS", "1")

    result = volume_management.lambda_handler({}, None)

    assert result["statusCode"] == 200
    assert len(setup_mock_volumes) == 3
    assert len(setup_mock_snapshots) == 3
