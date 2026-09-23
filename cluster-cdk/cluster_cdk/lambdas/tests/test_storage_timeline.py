import datetime

import boto3
import pytest
from unittest.mock import patch, MagicMock
from moto import mock_aws

import volume_management

AWS_REGION_NAME = "us-west-2"

NOW = datetime.datetime.strptime(
    "2000-01-01 12:30:00+0000", volume_management.DATE_FORMAT
)

# Note that AWs creds are nullified within the conftest.py file


class MockDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


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
def mock_k8s():
    """Fixture to mock kubernetes.client.CoreV1Api and load_kube_config. This allows us to bypass mocking the k8s cluster itself."""
    # Prevent the test from trying to load an actual local kubeconfig file
    # Patch CoreV1Api where it is imported/used in your application module
    patcher_client = patch("volume_management.k8s_client")
    patcher_config = patch("volume_management.k8s_config")

    # Activate the mocks
    mock_client = patcher_client.start()
    mock_config = patcher_config.start()

    mock_api = mock_client.CoreV1Api

    def mock_delete_namespaced_persistent_volume_claim(name, namespace="jupyter"):
        with mock_aws():
            ec2_resource = boto3.resource("ec2")

            # Cycle through volumes and get proper claim name
            vol_id = None
            volumes = ec2_resource.volumes.all()
            for volume in volumes:
                for tag in volume.tags:
                    if (
                        tag["Key"] == "kubernetes.io/created-for/pvc/name"
                        and tag["Value"] == name
                    ):
                        vol_id = volume.id
                        break

            if not vol_id:
                raise Exception(f"Volume not found for claim '{name}'")

            claimed_volume = ec2_resource.Volume(vol_id)
            claimed_volume.delete()

            return {"status": "Success"}

    mock_api.return_value.delete_namespaced_persistent_volume_claim.side_effect = (
        mock_delete_namespaced_persistent_volume_claim
    )

    yield {
        "client": mock_client,
        "config": mock_config,
        "api": mock_api,
    }

    # Cleanup runs after the test finishes, restoring original functionality
    patcher_client.stop()
    patcher_config.stop()


@pytest.fixture
def patched_volume_management(
    setup_mock_secret_manager,
    setup_mock_portal_post,
    monkeypatch,
):
    # Mock internal functions using monkeypatch
    monkeypatch.setattr("volume_management.datetime.datetime", MockDatetime)

    # Mock default parameters
    monkeypatch.setattr("volume_management.SSO_SECRET_ARN", setup_mock_secret_manager)
    monkeypatch.setattr("volume_management.SNS_ALERT_TOPIC_ARN", "")
    monkeypatch.setattr("volume_management.PORTAL_DOMAIN", "mock.cloudfront.net")


def mock_volumes(config: list) -> dict:
    """Context manager to provision volumes with various tags."""
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=AWS_REGION_NAME)

        created_volumes = {}
        for item in config:
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
                                "Value": item["volume-delete-time"],
                            },
                            {
                                "Key": "snapshot-delete-time",
                                "Value": item["snapshot-delete-time"],
                            },
                            {
                                "Key": volume_management.CLUSTER_TAG,
                                "Value": "mocklab",
                            },
                            {
                                "Key": volume_management.CLAIM_TAG,
                                "Value": item["claim_name"],
                            },
                        ],
                    }
                ],
            )
            created_volumes[item["name"]] = v

        return created_volumes


def mock_snapshots(
    config: list,
    mock_volumes: dict,
    remove_volume_after_snapshot_creation: bool = False,
) -> dict:
    """Context manager to provision EBS snapshots with various tags."""
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=AWS_REGION_NAME)

        created_snapshots = {}
        for item in config:
            associated_volume = item["associated"]
            volume_id = mock_volumes[associated_volume]["VolumeId"]

            s = ec2.create_snapshot(
                VolumeId=volume_id,
                TagSpecifications=[
                    {
                        "ResourceType": "snapshot",
                        "Tags": [
                            {"Key": "Name", "Value": item["name"]},
                            {
                                "Key": "volume-delete-time",
                                "Value": item["volume-delete-time"],
                            },
                            {
                                "Key": "snapshot-delete-time",
                                "Value": item["snapshot-delete-time"],
                            },
                            {
                                "Key": volume_management.CLUSTER_TAG,
                                "Value": "mocklab",
                            },
                            {
                                "Key": volume_management.CLAIM_TAG,
                                "Value": item["claim_name"],
                            },
                        ],
                    }
                ],
            )
            created_snapshots[item["name"]] = s

            if remove_volume_after_snapshot_creation:
                ec2.delete_volume(VolumeId=volume_id)

        return created_snapshots


# Various tests
def test_unexpired_volume_with_no_snapshot_and_do_keep_volume(
    mock_k8s, patched_volume_management, monkeypatch, caplog
):
    monkeypatch.setattr("volume_management.get_eks_api", mock_k8s["api"])
    monkeypatch.setattr("volume_management.LAB_SHORT_NAME", "mocklab")
    monkeypatch.setattr("volume_management.CLUSTER_NAME", "mocklab")
    monkeypatch.setattr("volume_management.SNAPSHOT_WARNING_DAYS", [1])

    volume_configs = [
        {
            "claim_name": "claim-mockuser0",
            "name": "new_volume",
            "volume-delete-time": "2000-01-03 00:00:00+0000",
            "snapshot-delete-time": "2000-01-30 00:00:00+0000",
        }
    ]

    mock_volumes(volume_configs)

    vols_before_run = volume_management.get_all_unattached_volumes_in_lab()
    assert len(vols_before_run) == 1

    snaps_before_run = volume_management.get_all_completed_snapshots_in_lab()
    assert len(snaps_before_run) == 0

    # Run lambda
    result = volume_management.lambda_handler({}, None)

    # Confirm expected results
    assert result["statusCode"] == 200

    vols_after_run = volume_management.get_all_unattached_volumes_in_lab()
    assert len(vols_after_run) == 1

    snaps_after_run = volume_management.get_all_completed_snapshots_in_lab()
    assert len(snaps_after_run) == 0


def test_expired_volume_with_no_snapshot_and_do_keep_volume(
    mock_k8s, patched_volume_management, monkeypatch, caplog
):
    monkeypatch.setattr("volume_management.get_eks_api", mock_k8s["api"])
    monkeypatch.setattr("volume_management.LAB_SHORT_NAME", "mocklab")
    monkeypatch.setattr("volume_management.CLUSTER_NAME", "mocklab")
    monkeypatch.setattr("volume_management.SNAPSHOT_WARNING_DAYS", [1])

    volume_configs = [
        {
            "claim_name": "claim-mockuser0",
            "name": "new_volume",
            "volume-delete-time": "1999-12-15 00:00:00+0000",
            "snapshot-delete-time": "2000-01-15 00:00:00+0000",
        }
    ]

    mock_volumes(volume_configs)

    vols_before_run = volume_management.get_all_unattached_volumes_in_lab()
    assert len(vols_before_run) == 1

    snaps_before_run = volume_management.get_all_completed_snapshots_in_lab()
    assert len(snaps_before_run) == 0

    # Run lambda
    result = volume_management.lambda_handler({}, None)

    # Confirm expected results
    assert result["statusCode"] == 200

    vols_after_run = volume_management.get_all_unattached_volumes_in_lab()
    assert len(vols_after_run) == 1

    snaps_after_run = volume_management.get_all_completed_snapshots_in_lab()
    assert len(snaps_after_run) == 0


def test_expired_volume_with_unexpired_snapshot_and_do_delete_volume(
    mock_k8s, patched_volume_management, monkeypatch, caplog
):
    monkeypatch.setattr("volume_management.get_eks_api", mock_k8s["api"])
    monkeypatch.setattr("volume_management.LAB_SHORT_NAME", "mocklab")
    monkeypatch.setattr("volume_management.CLUSTER_NAME", "mocklab")
    monkeypatch.setattr("volume_management.SNAPSHOT_WARNING_DAYS", [1])

    volume_configs = [
        {
            "claim_name": "claim-mockuser0",
            "name": "new_volume",
            "volume-delete-time": "1999-12-15 00:00:00+0000",
            "snapshot-delete-time": "2000-01-15 00:00:00+0000",
        }
    ]

    vols = mock_volumes(volume_configs)

    snapshot_configs = [
        {
            "claim_name": "claim-mockuser0",
            "name": "new_snap",
            "associated": "new_volume",
            "volume-delete-time": "1999-12-15 00:00:00+0000",
            "snapshot-delete-time": "2000-01-15 00:00:00+0000",
        }
    ]

    mock_snapshots(snapshot_configs, vols)

    vols_before_run = volume_management.get_all_unattached_volumes_in_lab()
    assert len(vols_before_run) == 1

    snaps_before_run = volume_management.get_all_completed_snapshots_in_lab()
    assert len(snaps_before_run) == 1

    # Run lambda
    result = volume_management.lambda_handler({}, None)

    # Confirm expected results
    assert result["statusCode"] == 200

    vols_after_run = volume_management.get_all_unattached_volumes_in_lab()
    assert len(vols_after_run) == 0
    assert "Volume is expired!" in caplog.text

    snaps_after_run = volume_management.get_all_completed_snapshots_in_lab()
    assert len(snaps_after_run) == 1


def test_unexpired_snapshot_with_no_volume_and_do_keep_snapshot(
    mock_k8s, patched_volume_management, monkeypatch, caplog
):
    monkeypatch.setattr("volume_management.get_eks_api", mock_k8s["api"])
    monkeypatch.setattr("volume_management.LAB_SHORT_NAME", "mocklab")
    monkeypatch.setattr("volume_management.CLUSTER_NAME", "mocklab")
    monkeypatch.setattr("volume_management.SNAPSHOT_WARNING_DAYS", [1])

    # Create initial volume that the snapshot will be based off
    volume_configs = [
        {
            "claim_name": "claim-mockuser0",
            "name": "new_volume",
            "volume-delete-time": "1999-12-15 00:00:00+0000",
            "snapshot-delete-time": "2000-01-15 00:00:00+0000",
        }
    ]

    vols = mock_volumes(volume_configs)

    snapshot_configs = [
        {
            "claim_name": "claim-mockuser0",
            "name": "new_snap",
            "associated": "new_volume",
            "volume-delete-time": "1999-12-15 00:00:00+0000",
            "snapshot-delete-time": "2000-01-15 00:00:00+0000",
        }
    ]

    mock_snapshots(
        snapshot_configs,
        vols,
        remove_volume_after_snapshot_creation=True,
    )

    vols_before_run = volume_management.get_all_unattached_volumes_in_lab()
    assert len(vols_before_run) == 0

    snaps_before_run = volume_management.get_all_completed_snapshots_in_lab()
    assert len(snaps_before_run) == 1

    # Run lambda
    result = volume_management.lambda_handler({}, None)

    # Confirm expected results
    assert result["statusCode"] == 200

    vols_after_run = volume_management.get_all_unattached_volumes_in_lab()
    assert len(vols_after_run) == 0

    snaps_after_run = volume_management.get_all_completed_snapshots_in_lab()
    assert len(snaps_after_run) == 1


def test_duplicate_unexpired_snapshot_with_no_volume_and_do_delete_duplicate(
    mock_k8s, patched_volume_management, monkeypatch, caplog
):
    monkeypatch.setattr("volume_management.get_eks_api", mock_k8s["api"])
    monkeypatch.setattr("volume_management.LAB_SHORT_NAME", "mocklab")
    monkeypatch.setattr("volume_management.CLUSTER_NAME", "mocklab")
    monkeypatch.setattr("volume_management.SNAPSHOT_WARNING_DAYS", [1])

    # Created volume and duplicate snapshots and then delete volume
    vols_duplicated = mock_volumes(
        [
            {
                "claim_name": "claim-mockuser0",
                "name": "mockuser0_volume",
                "volume-delete-time": "1999-12-15 00:00:00+0000",
                "snapshot-delete-time": "2000-01-15 00:00:00+0000",
            },
        ]
    )

    mock_snapshots(
        [
            {
                "claim_name": "claim-mockuser0",
                "name": "new_snap",
                "associated": "mockuser0_volume",
                "volume-delete-time": "1999-12-15 00:00:00+0000",
                "snapshot-delete-time": "2000-01-15 00:00:00+0000",
            },
        ],
        vols_duplicated,
        # Don't delete volume so it can be used again for the duplicate snapshot
        remove_volume_after_snapshot_creation=False,
    )

    mock_snapshots(
        [
            {
                "claim_name": "claim-mockuser0",
                "name": "duplicate_snap",
                "associated": "mockuser0_volume",
                "volume-delete-time": "1999-12-15 00:00:00+0000",
                "snapshot-delete-time": "2000-01-15 00:00:00+0000",
            },
        ],
        vols_duplicated,
        remove_volume_after_snapshot_creation=True,
    )

    # Create normal volume, take non-duplicate snapshot as a control, and then delete volume
    vols_not_duplicated = mock_volumes(
        [
            {
                "claim_name": "claim-mockuser1",
                "name": "mockuser1_volume",
                "volume-delete-time": "1999-12-20 00:00:00+0000",
                "snapshot-delete-time": "2000-01-20 00:00:00+0000",
            },
        ]
    )

    mock_snapshots(
        [
            {
                "claim_name": "claim-mockuser1",
                "name": "other_snap",
                "associated": "mockuser1_volume",
                "volume-delete-time": "1999-12-20 00:00:00+0000",
                "snapshot-delete-time": "2000-01-20 00:00:00+0000",
            },
        ],
        vols_not_duplicated,
        remove_volume_after_snapshot_creation=True,
    )

    # Confirm number of volumes and snapshots
    vols_before_run = volume_management.get_all_unattached_volumes_in_lab()
    assert len(vols_before_run) == 0

    snaps_before_run = volume_management.get_all_completed_snapshots_in_lab()
    assert len(snaps_before_run) == 3

    # Run lambda
    result = volume_management.lambda_handler({}, None)

    # Confirm expected results
    assert result["statusCode"] == 200

    vols_after_run = volume_management.get_all_unattached_volumes_in_lab()
    assert len(vols_after_run) == 0

    snaps_after_run = volume_management.get_all_completed_snapshots_in_lab()
    assert len(snaps_after_run) == 2
    assert "claim-mockuser0" == volume_management.get_claim_name(snaps_after_run[0])
    assert "claim-mockuser1" == volume_management.get_claim_name(snaps_after_run[1])
    assert "Duplicate snapshot found. Deleting " in caplog.text


def test_almost_expired_snapshot_with_no_volume_and_do_send_warning(
    mock_k8s,
    patched_volume_management,
    monkeypatch,
    caplog,
):
    monkeypatch.setattr("volume_management.get_eks_api", mock_k8s["api"])
    monkeypatch.setattr("volume_management.LAB_SHORT_NAME", "mocklab")
    monkeypatch.setattr("volume_management.CLUSTER_NAME", "mocklab")
    monkeypatch.setattr("volume_management.SNAPSHOT_WARNING_DAYS", [1])

    # Create initial volume that the snapshot will be based off
    volume_configs = [
        {
            "claim_name": "claim-mockuser0",
            "name": "new_volume",
            "volume-delete-time": "1999-12-01 00:00:00+0000",
            "snapshot-delete-time": "2000-01-02 00:00:00+0000",
        }
    ]

    vols = mock_volumes(volume_configs)

    snapshot_configs = [
        {
            "claim_name": "claim-mockuser0",
            "name": "new_snap",
            "associated": "new_volume",
            "volume-delete-time": "1999-12-01 00:00:00+0000",
            "snapshot-delete-time": "2000-01-02 00:00:00+0000",
        }
    ]

    mock_snapshots(
        snapshot_configs,
        vols,
        remove_volume_after_snapshot_creation=True,
    )

    vols_before_run = volume_management.get_all_unattached_volumes_in_lab()
    assert len(vols_before_run) == 0

    snaps_before_run = volume_management.get_all_completed_snapshots_in_lab()
    assert len(snaps_before_run) == 1

    # Run lambda
    result = volume_management.lambda_handler({}, None)

    # Confirm expected results
    assert result["statusCode"] == 200

    vols_after_run = volume_management.get_all_unattached_volumes_in_lab()
    assert len(vols_after_run) == 0

    snaps_after_run = volume_management.get_all_completed_snapshots_in_lab()
    assert len(snaps_after_run) == 1
    assert "Sending a snapshot warning email!" in caplog.text
    assert "Snapshot is in grace period!" not in caplog.text
    assert "Deletion email sent" not in caplog.text


def test_almost_expired_snapshot_with_just_restored_volume_and_do_not_send_warning(
    mock_k8s,
    patched_volume_management,
    monkeypatch,
    caplog,
):
    monkeypatch.setattr("volume_management.get_eks_api", mock_k8s["api"])
    monkeypatch.setattr("volume_management.LAB_SHORT_NAME", "mocklab")
    monkeypatch.setattr("volume_management.CLUSTER_NAME", "mocklab")
    monkeypatch.setattr("volume_management.SNAPSHOT_WARNING_DAYS", [1])

    # Create initial volume that the snapshot will be based off
    volume_configs = [
        {
            "claim_name": "claim-mockuser0",
            "name": "new_volume",
            "volume-delete-time": "2000-01-15 00:00:00+0000",
            "snapshot-delete-time": "2000-01-02 00:00:00+0000",
        }
    ]

    vols = mock_volumes(volume_configs)

    snapshot_configs = [
        {
            "claim_name": "claim-mockuser0",
            "name": "new_snap",
            "associated": "new_volume",
            "volume-delete-time": "2000-01-15 00:00:00+0000",
            "snapshot-delete-time": "2000-01-02 00:00:00+0000",
        }
    ]

    mock_snapshots(
        snapshot_configs,
        vols,
        remove_volume_after_snapshot_creation=False,
    )

    vols_before_run = volume_management.get_all_unattached_volumes_in_lab()
    assert len(vols_before_run) == 1

    snaps_before_run = volume_management.get_all_completed_snapshots_in_lab()
    assert len(snaps_before_run) == 1

    # Run lambda
    result = volume_management.lambda_handler({}, None)

    # Confirm expected results
    assert result["statusCode"] == 200

    vols_after_run = volume_management.get_all_unattached_volumes_in_lab()
    assert len(vols_after_run) == 1

    snaps_after_run = volume_management.get_all_completed_snapshots_in_lab()
    assert len(snaps_after_run) == 1
    assert "Snapshot is in grace period!" not in caplog.text
    assert "Deletion email sent" not in caplog.text
    assert "Deletion email sent" not in caplog.text
    assert "Sending a snapshot warning email!" not in caplog.text


def test_expired_snapshot_with_no_volume_and_within_grace_period_and_do_not_delete_snapshot(
    mock_k8s, patched_volume_management, monkeypatch, caplog
):
    monkeypatch.setattr("volume_management.get_eks_api", mock_k8s["api"])
    monkeypatch.setattr("volume_management.LAB_SHORT_NAME", "mocklab")
    monkeypatch.setattr("volume_management.CLUSTER_NAME", "mocklab")
    monkeypatch.setattr("volume_management.SNAPSHOT_WARNING_DAYS", [1])
    monkeypatch.setattr("volume_management.SNAPSHOT_GRACEPERIOD_DAYS", 1)

    # Create initial volume that the snapshot will be based off
    volume_configs = [
        {
            "claim_name": "claim-mockuser0",
            "name": "new_volume",
            "volume-delete-time": "1999-11-01 00:00:00+0000",
            "snapshot-delete-time": "1999-12-31 18:00:00+0000",
        }
    ]

    vols = mock_volumes(volume_configs)

    snapshot_configs = [
        {
            "claim_name": "claim-mockuser0",
            "name": "new_snap",
            "associated": "new_volume",
            "volume-delete-time": "1999-11-01 00:00:00+0000",
            "snapshot-delete-time": "1999-12-31 18:00:00+0000",
        }
    ]

    mock_snapshots(
        snapshot_configs,
        vols,
        remove_volume_after_snapshot_creation=True,
    )

    vols_before_run = volume_management.get_all_unattached_volumes_in_lab()
    assert len(vols_before_run) == 0

    snaps_before_run = volume_management.get_all_completed_snapshots_in_lab()
    assert len(snaps_before_run) == 1

    # Run lambda
    result = volume_management.lambda_handler({}, None)

    # Confirm expected results
    assert result["statusCode"] == 200

    vols_after_run = volume_management.get_all_unattached_volumes_in_lab()
    assert len(vols_after_run) == 0

    snaps_after_run = volume_management.get_all_completed_snapshots_in_lab()
    assert len(snaps_after_run) == 1
    assert "Snapshot is in grace period!" in caplog.text
    assert "Deleting Snapshot" not in caplog.text
    assert "Sending a snapshot warning email!" not in caplog.text
    assert "Deletion email sent" in caplog.text


def test_expired_snapshot_with_no_volume_and_beyond_grace_period_and_do_delete_snapshot(
    mock_k8s, patched_volume_management, monkeypatch, caplog
):
    monkeypatch.setattr("volume_management.get_eks_api", mock_k8s["api"])
    monkeypatch.setattr("volume_management.LAB_SHORT_NAME", "mocklab")
    monkeypatch.setattr("volume_management.CLUSTER_NAME", "mocklab")
    monkeypatch.setattr("volume_management.SNAPSHOT_WARNING_DAYS", [1])

    # Create initial volume that the snapshot will be based off
    volume_configs = [
        {
            "claim_name": "claim-mockuser0",
            "name": "new_volume",
            "volume-delete-time": "1999-11-01 00:00:00+0000",
            "snapshot-delete-time": "1999-12-01 00:00:00+0000",
        }
    ]

    vols = mock_volumes(volume_configs)

    snapshot_configs = [
        {
            "claim_name": "claim-mockuser0",
            "name": "new_snap",
            "associated": "new_volume",
            "volume-delete-time": "1999-11-01 00:00:00+0000",
            "snapshot-delete-time": "1999-12-01 00:00:00+0000",
        }
    ]

    mock_snapshots(
        snapshot_configs,
        vols,
        remove_volume_after_snapshot_creation=True,
    )

    vols_before_run = volume_management.get_all_unattached_volumes_in_lab()
    assert len(vols_before_run) == 0

    snaps_before_run = volume_management.get_all_completed_snapshots_in_lab()
    assert len(snaps_before_run) == 1

    # Run lambda
    result = volume_management.lambda_handler({}, None)

    # Confirm expected results
    assert result["statusCode"] == 200

    vols_after_run: list = volume_management.get_all_unattached_volumes_in_lab()
    assert len(vols_after_run) == 0

    snaps_after_run: list = volume_management.get_all_completed_snapshots_in_lab()
    assert len(snaps_after_run) == 0
    assert "Deleting Snapshot" in caplog.text
    assert "Deletion email sent" not in caplog.text
    assert "Sending a snapshot warning email!" not in caplog.text


def test_expired_snapshot_with_restored_volume_and_beyond_grace_period_and_do_not_delete_snapshot(
    mock_k8s, patched_volume_management, monkeypatch, caplog
):
    monkeypatch.setattr("volume_management.get_eks_api", mock_k8s["api"])
    monkeypatch.setattr("volume_management.LAB_SHORT_NAME", "mocklab")
    monkeypatch.setattr("volume_management.CLUSTER_NAME", "mocklab")
    monkeypatch.setattr("volume_management.SNAPSHOT_WARNING_DAYS", [1])

    # Create initial volume that the snapshot will be based off
    volume_configs = [
        {
            "claim_name": "claim-mockuser0",
            "name": "new_volume",
            "volume-delete-time": "2000-11-01 00:00:00+0000",
            "snapshot-delete-time": "1999-12-01 00:00:00+0000",
        }
    ]

    vols = mock_volumes(volume_configs)

    snapshot_configs = [
        {
            "claim_name": "claim-mockuser0",
            "name": "new_snap",
            "associated": "new_volume",
            "volume-delete-time": "2000-11-01 00:00:00+0000",
            "snapshot-delete-time": "1999-12-01 00:00:00+0000",
        }
    ]

    mock_snapshots(
        snapshot_configs,
        vols,
        remove_volume_after_snapshot_creation=False,
    )

    vols_before_run = volume_management.get_all_unattached_volumes_in_lab()
    assert len(vols_before_run) == 1

    snaps_before_run = volume_management.get_all_completed_snapshots_in_lab()
    assert len(snaps_before_run) == 1

    # Run lambda
    result = volume_management.lambda_handler({}, None)

    # Confirm expected results
    assert result["statusCode"] == 200

    vols_after_run: list = volume_management.get_all_unattached_volumes_in_lab()
    assert len(vols_after_run) == 1

    snaps_after_run: list = volume_management.get_all_completed_snapshots_in_lab()
    assert len(snaps_after_run) == 1
    assert "Deletion email sent" not in caplog.text
    assert "Snapshot is in grace period!" not in caplog.text
    assert "Deleting Snapshot" not in caplog.text
    assert "Sending a snapshot warning email!" not in caplog.text
