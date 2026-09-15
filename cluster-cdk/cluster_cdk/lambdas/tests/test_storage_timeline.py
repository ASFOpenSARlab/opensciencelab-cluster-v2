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


def mock_volumes(config: list) -> dict:
    """Context manager to provision volumes with various tags."""
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=AWS_REGION_NAME)

        created_volumes = {}
        for num, item in enumerate(config):
            v = ec2.create_volume(
                AvailabilityZone=f"{AWS_REGION_NAME}a",
                Size=10,
                TagSpecifications=[
                    {
                        "ResourceType": "volume",
                        "Tags": [
                            {"Key": "Name", "Value": item["name"]},
                            {
                                "Key": "volume-delete-time",
                                "Value": f"{NOW + datetime.timedelta(days=item['vol_days'])}",
                            },
                            {
                                "Key": "snapshot-delete-time",
                                "Value": f"{NOW + datetime.timedelta(days=item['snap_days'])}",
                            },
                            {
                                "Key": volume_management.CLUSTER_TAG,
                                "Value": "mocklab",
                            },
                            {
                                "Key": volume_management.CLAIM_TAG,
                                "Value": f"claim-mockuser{num}",
                            },
                        ],
                    }
                ],
            )
            created_volumes[item["name"]] = v

        return created_volumes


def mock_snapshots(config: list, mock_volumes: dict) -> dict:
    """Context manager to provision EBS snapshots with various tags."""
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=AWS_REGION_NAME)

        created_snapshots = {}
        for num, item in enumerate(config):
            associated_volume = item["associated"]
            s = ec2.create_snapshot(
                VolumeId=mock_volumes[associated_volume]["VolumeId"],
                TagSpecifications=[
                    {
                        "ResourceType": "snapshot",
                        "Tags": [
                            {"Key": "Name", "Value": item["name"]},
                            {
                                "Key": "volume-delete-time",
                                "Value": f"{NOW + datetime.timedelta(days=item['vol_days'])}",
                            },
                            {
                                "Key": "snapshot-delete-time",
                                "Value": f"{NOW + datetime.timedelta(days=item['snap_days'])}",
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
                    }
                ],
            )
            created_snapshots[item["name"]] = s

        return created_snapshots


# Various tests
def test_new_volume_no_snapshot(
    patched_volume_management,
    monkeypatch,
):
    """New volume created with no snapshot"""
    volume_configs = [{"name": "new_volume", "vol_days": 2, "snap_days": 28}]

    vols = mock_volumes(volume_configs)

    monkeypatch.setattr("volume_management.LAB_SHORT_NAME", "mocklab")
    monkeypatch.setattr("volume_management.CLUSTER_NAME", "mocklab")
    monkeypatch.setattr("volume_management.SNAPSHOT_WARNING_DAYS", [1])

    # Run lambda
    result = volume_management.lambda_handler({}, None)

    # Results
    assert result["statusCode"] == 200
    assert len(vols) == 1


def test_expired_volume_no_snapshot(
    patched_volume_management,
    monkeypatch,
):
    """Expired volume with no snapshot"""
    volume_configs = [{"name": "new_volume", "vol_days": -1, "snap_days": 25}]

    vols = mock_volumes(volume_configs)

    monkeypatch.setattr("volume_management.LAB_SHORT_NAME", "mocklab")
    monkeypatch.setattr("volume_management.CLUSTER_NAME", "mocklab")
    monkeypatch.setattr("volume_management.SNAPSHOT_WARNING_DAYS", [1])

    # Run lambda
    result = volume_management.lambda_handler({}, None)

    # Results
    assert result["statusCode"] == 200
    assert len(vols) == 1


def test_expired_volume_with_snapshot(
    patched_volume_management,
    monkeypatch,
):
    """Expired volume with no snapshot"""
    volume_configs = [{"name": "new_volume", "vol_days": -1, "snap_days": 25}]

    vols = mock_volumes(volume_configs)

    snapshot_configs = [
        {
            "name": "new_snap",
            "associated": "new_volume",
            "vol_days": -1,
            "snap_days": 25,
        }
    ]

    snaps = mock_snapshots(snapshot_configs, vols)

    monkeypatch.setattr("volume_management.LAB_SHORT_NAME", "mocklab")
    monkeypatch.setattr("volume_management.CLUSTER_NAME", "mocklab")
    monkeypatch.setattr("volume_management.SNAPSHOT_WARNING_DAYS", [1])

    # Run lambda
    result = volume_management.lambda_handler({}, None)

    # Results
    assert result["statusCode"] == 200
    assert len(vols) == 0
    assert len(snaps) == 1
