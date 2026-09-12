import datetime

import boto3
import pytest
from unittest.mock import patch, MagicMock
from moto import mock_aws

import volume_management

AWS_REGION_NAME = "us-west-2"

NOW = datetime.datetime.strptime(
    "2000-01-01 00:00:00+0000", volume_management.DATE_FORMAT
)

# Note that AWs creds are nullified within the conftest.py file


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
    """Bypass the normal requests POST call to the portal email service"""
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
        """Fixture to mock kubernetes.client.CoreV1Api and load_kube_config. This allows us to bypass mocking the k8s cluster itself."""
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
    monkeypatch.setattr("volume_management.SSO_SECRET_ARN", setup_mock_secret_manager)
    monkeypatch.setattr("volume_management.SNS_ALERT_TOPIC_ARN", "")
    monkeypatch.setattr("volume_management.PORTAL_DOMAIN", "mock.cloudfront.net")


@pytest.fixture
def mock_volumes():
    """Context manager to provision volumes with various tags."""
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=AWS_REGION_NAME)

        volume_configs = [
            {
                "name": "new_volume",
                "tags": [
                    {"Key": "Name", "Value": "new_volume"},
                    {
                        "Key": "volume-delete-time",
                        "Value": f"{NOW + datetime.timedelta(days=2)}",
                    },
                    {
                        "Key": "snapshot-delete-time",
                        "Value": f"{NOW + datetime.timedelta(days=28)}",
                    },
                    {
                        "Key": volume_management.CLUSTER_TAG,
                        "Value": "mocklab",
                    },
                    {
                        "Key": volume_management.CLAIM_TAG,
                        "Value": "claim-mockuser1",
                    },
                ],
            },
            {
                "name": "another_volume",
                "tags": [
                    {"Key": "Name", "Value": "another_volume"},
                    {
                        "Key": "volume-delete-time",
                        "Value": f"{NOW + datetime.timedelta(days=2)}",
                    },
                    {
                        "Key": "snapshot-delete-time",
                        "Value": f"{NOW + datetime.timedelta(days=28)}",
                    },
                    {
                        "Key": volume_management.CLUSTER_TAG,
                        "Value": "mocklab",
                    },
                    {
                        "Key": volume_management.CLAIM_TAG,
                        "Value": "claim-mockuser2",
                    },
                ],
            },
            {
                "name": "expired_volume",
                "tags": [
                    {"Key": "Name", "Value": "expired_volume"},
                    {
                        "Key": "volume-delete-time",
                        "Value": f"{NOW + datetime.timedelta(days=0)}",
                    },
                    {
                        "Key": "snapshot-delete-time",
                        "Value": f"{NOW + datetime.timedelta(days=26)}",
                    },
                    {
                        "Key": volume_management.CLUSTER_TAG,
                        "Value": "mocklab",
                    },
                    {
                        "Key": volume_management.CLAIM_TAG,
                        "Value": "claim-mockuser3",
                    },
                ],
            },
            {
                "name": "protected_expired_volume",
                "tags": [
                    {"Key": "Name", "Value": "protected_expired_volume"},
                    {"Key": "do-not-delete", "Value": "true"},
                    {
                        "Key": "volume-delete-time",
                        "Value": f"{NOW + datetime.timedelta(days=-1)}",
                    },
                    {
                        "Key": "snapshot-delete-time",
                        "Value": f"{NOW + datetime.timedelta(days=27)}",
                    },
                    {
                        "Key": volume_management.CLUSTER_TAG,
                        "Value": "mocklab",
                    },
                    {
                        "Key": volume_management.CLAIM_TAG,
                        "Value": "claim-mockuser4",
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
def mock_snapshots(mock_volumes):
    """Context manager to provision snapshots with various tags."""
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=AWS_REGION_NAME)

        snapshot_configs = [
            {
                "name": "new_snapshot",
                "associated_volume": "new_volume",
                "tags": [
                    {"Key": "Name", "Value": "new_snapshot"},
                    {
                        "Key": "volume-delete-time",
                        "Value": f"{NOW + datetime.timedelta(days=1)}",
                    },
                    {
                        "Key": "snapshot-delete-time",
                        "Value": f"{NOW + datetime.timedelta(days=27)}",
                    },
                    {
                        "Key": volume_management.CLUSTER_TAG,
                        "Value": "mocklab",
                    },
                    {
                        "Key": volume_management.CLAIM_TAG,
                        "Value": "claim-mockuser1",
                    },
                ],
            },
            {
                "name": "older_snapshot",
                "associated_volume": "another_volume",
                "tags": [
                    {"Key": "Name", "Value": "older_snapshot"},
                    {
                        "Key": "volume-delete-time",
                        "Value": f"{NOW + datetime.timedelta(days=-14)}",
                    },
                    {
                        "Key": "snapshot-delete-time",
                        "Value": f"{NOW + datetime.timedelta(days=14)}",
                    },
                    {
                        "Key": volume_management.CLUSTER_TAG,
                        "Value": "mocklab",
                    },
                    {
                        "Key": volume_management.CLAIM_TAG,
                        "Value": "claim-mockuser2",
                    },
                ],
            },
            {
                "name": "expired_snapshot",
                "associated_volume": "expired_volume",
                "tags": [
                    {"Key": "Name", "Value": "expired_snapshot"},
                    {
                        "Key": "volume-delete-time",
                        "Value": f"{NOW + datetime.timedelta(days=-28)}",
                    },
                    {
                        "Key": "snapshot-delete-time",
                        "Value": f"{NOW + datetime.timedelta(days=0)}",
                    },
                    {
                        "Key": volume_management.CLUSTER_TAG,
                        "Value": "mocklab",
                    },
                    {
                        "Key": volume_management.CLAIM_TAG,
                        "Value": "claim-mockuser3",
                    },
                ],
            },
            {
                "name": "past_snapshot",
                "associated_volume": "expired_volume",
                "tags": [
                    {"Key": "Name", "Value": "past_snapshot"},
                    {
                        "Key": "volume-delete-time",
                        "Value": f"{NOW + datetime.timedelta(days=-30)}",
                    },
                    {
                        "Key": "snapshot-delete-time",
                        "Value": f"{NOW + datetime.timedelta(days=-2)}",
                    },
                    {
                        "Key": volume_management.CLUSTER_TAG,
                        "Value": "mocklab",
                    },
                    {
                        "Key": volume_management.CLAIM_TAG,
                        "Value": "claim-mockuser3",
                    },
                ],
            },
        ]

        created_snapshots = {}
        for config in snapshot_configs:
            s = ec2.create_snapshot(
                VolumeId=mock_volumes["new_volume"]["VolumeId"],
                TagSpecifications=[
                    {"ResourceType": "snapshot", "Tags": config["tags"]}
                ],
            )
            created_snapshots[config["name"]] = s
        yield created_snapshots


def test_new_volume_no_shapshot(
    mock_volumes,
    mock_snapshots,
    monkeypatch,
    patched_volume_management,
):
    """New volume created with no snapshot"""
    monkeypatch.setattr("volume_management.LAB_SHORT_NAME", "mocklab")
    monkeypatch.setattr("volume_management.CLUSTER_NAME", "mocklab")
    monkeypatch.setattr("volume_management.SNAPSHOT_WARNING_DAYS", [1])

    result = volume_management.lambda_handler({}, None)

    assert result["statusCode"] == 200
    assert len(mock_volumes) == 4
    assert len(mock_snapshots) == 3
