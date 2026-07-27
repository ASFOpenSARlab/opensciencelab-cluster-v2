from unittest.mock import patch
from moto import mock_aws
import datetime

from cluster_cdk.lambdas.volume_management import (
    DATE_FORMAT,
    should_send_snapshot_warning_email,
    send_snapshot_warning
)


class mock_snapshot:
    tags = []

    @classmethod
    def create_tags(self, *args, **kwargs):
        return NotImplementedError


@mock_aws
class TestShouldSendSnapshotWarning:
    def test_no_warning_sent_before_first_warning_time(self, monkeypatch):
        class MockDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.datetime.strptime(
                    "2026-01-10 01:00:00+00:00", DATE_FORMAT
                )

        monkeypatch.setattr(
            "cluster_cdk.lambdas.volume_management.datetime.datetime", MockDatetime
        )

        MONKEYPATCH_SNAPSHOT_WARNING_DAYS = [10, 5, 3, 1]
        monkeypatch.setattr(
            "cluster_cdk.lambdas.volume_management.SNAPSHOT_WARNING_DAYS",
            MONKEYPATCH_SNAPSHOT_WARNING_DAYS
        )
 
        snap = mock_snapshot()
        snap.tags = [
            {
                "Key": "snapshot-delete-time", "Value": "2026-01-30 01:00:00+00:00"
            }
        ]

        should_send = should_send_snapshot_warning_email(snap)
        assert not should_send

    def test_no_warning_sent_after_first_warning_time(self, monkeypatch):
        class MockDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.datetime.strptime(
                    "2026-01-21 01:00:00+00:00", DATE_FORMAT
                )

        monkeypatch.setattr(
            "cluster_cdk.lambdas.volume_management.datetime.datetime", MockDatetime
        )

        MONKEYPATCH_SNAPSHOT_WARNING_DAYS = [10, 5, 3, 1]
        monkeypatch.setattr(
            "cluster_cdk.lambdas.volume_management.SNAPSHOT_WARNING_DAYS",
            MONKEYPATCH_SNAPSHOT_WARNING_DAYS
        )
 
        snap = mock_snapshot()
        snap.tags = [
            {
                "Key": "snapshot-delete-time", "Value": "2026-01-30 01:00:00+00:00"
            }
        ]

        should_send = should_send_snapshot_warning_email(snap)
        assert should_send

    def test_one_warning_sent_after_first_warning_time(self, monkeypatch):
        class MockDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.datetime.strptime(
                    "2026-01-22 01:00:00+00:00", DATE_FORMAT
                )

        monkeypatch.setattr(
            "cluster_cdk.lambdas.volume_management.datetime.datetime", MockDatetime
        )

        MONKEYPATCH_SNAPSHOT_WARNING_DAYS = [10, 5, 3, 1]
        monkeypatch.setattr(
            "cluster_cdk.lambdas.volume_management.SNAPSHOT_WARNING_DAYS",
            MONKEYPATCH_SNAPSHOT_WARNING_DAYS
        )
 
        snap = mock_snapshot()
        snap.tags = [
            {
                "Key": "snapshot-delete-time", "Value": "2026-01-30 01:00:00+00:00"
            },
            {
                "Key": "last-snapshot-warning-date", "Value": "2026-01-21 01:00:00+00:00"
            }
        ]

        should_send = should_send_snapshot_warning_email(snap)
        assert not should_send

    def test_one_warning_sent_after_second_warning_time(self, monkeypatch):
        class MockDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.datetime.strptime(
                    "2026-01-26 01:00:00+00:00", DATE_FORMAT
                )

        monkeypatch.setattr(
            "cluster_cdk.lambdas.volume_management.datetime.datetime", MockDatetime
        )

        MONKEYPATCH_SNAPSHOT_WARNING_DAYS = [10, 5, 3, 1]
        monkeypatch.setattr(
            "cluster_cdk.lambdas.volume_management.SNAPSHOT_WARNING_DAYS",
            MONKEYPATCH_SNAPSHOT_WARNING_DAYS
        )
 
        snap = mock_snapshot()
        snap.tags = [
            {
                "Key": "snapshot-delete-time", "Value": "2026-01-30 01:00:00+00:00"
            },
            {
                "Key": "last-snapshot-warning-date", "Value": "2026-01-21 01:00:00+00:00"
            }
        ]

        should_send = should_send_snapshot_warning_email(snap)
        assert should_send

    def test_one_warning_sent_after_third_warning_time(self, monkeypatch):
        class MockDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.datetime.strptime(
                    "2026-01-29 01:00:00+00:00", DATE_FORMAT
                )

        monkeypatch.setattr(
            "cluster_cdk.lambdas.volume_management.datetime.datetime", MockDatetime
        )

        MONKEYPATCH_SNAPSHOT_WARNING_DAYS = [10, 5, 3, 1]
        monkeypatch.setattr(
            "cluster_cdk.lambdas.volume_management.SNAPSHOT_WARNING_DAYS",
            MONKEYPATCH_SNAPSHOT_WARNING_DAYS
        )
 
        snap = mock_snapshot()
        snap.tags = [
            {
                "Key": "snapshot-delete-time", "Value": "2026-01-30 01:00:00+00:00"
            },
            {
                "Key": "last-snapshot-warning-date", "Value": "2026-01-21 01:00:00+00:00"
            }
        ]

        should_send = should_send_snapshot_warning_email(snap)
        assert should_send

    def test_all_warning_sent_after_all_warning_time(self, monkeypatch):
        class MockDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.datetime.strptime(
                    "2026-02-10 01:00:00+00:00", DATE_FORMAT
                )

        monkeypatch.setattr(
            "cluster_cdk.lambdas.volume_management.datetime.datetime", MockDatetime
            )

        MONKEYPATCH_SNAPSHOT_WARNING_DAYS = [10, 5, 3, 1]
        monkeypatch.setattr(
            "cluster_cdk.lambdas.volume_management.SNAPSHOT_WARNING_DAYS",
            MONKEYPATCH_SNAPSHOT_WARNING_DAYS
        )

        snap = mock_snapshot()
        snap.tags = [
            {
                "Key": "snapshot-delete-time",
                "Value": "2026-01-30 01:00:00+00:00"
            },
            {
                "Key": "last-snapshot-warning-date",
                "Value": "2026-01-30 02:00:00+00:00"
            }
        ]

        should_send = should_send_snapshot_warning_email(snap)
        assert not should_send


@mock_aws
class TestSendSnapshotWarning:
    def test_send_snapshot_warning(self, monkeypatch):
        snap = mock_snapshot()

        class MockDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.datetime.strptime(
                    "2026-01-10 01:00:00+00:00", DATE_FORMAT
                )
        monkeypatch.setattr(
            "cluster_cdk.lambdas.volume_management.datetime.datetime", MockDatetime
        )

        monkeypatch.setattr(
            "cluster_cdk.lambdas.volume_management.send_email_to_portal",
            lambda *args, **kwargs: None
        )

        claim_user = "testuser"
        with patch.object(mock_snapshot, "create_tags", autospec=True) as m:
            success = send_snapshot_warning(snap, claim_user)
            m.assert_called_once_with(
                Tags=[
                    {
                        'Key': 'last-snapshot-warning-date',
                        'Value': '2026-01-10 01:00:00+00:00',
                    }
                ]
            )

        assert success
