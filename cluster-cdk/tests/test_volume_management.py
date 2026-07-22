import pytest
from moto import mock_aws
import datetime

from cluster_cdk.lambdas.volume_management import should_send_snapshot_warning_email, DATE_FORMAT

class mock_snapshot:
    tags={}

@mock_aws
class TestVolumeManagement:
    def test_should_send_snapshot_warning_email_no_warning_sent_before_first_warning_time(self, monkeypatch):
        class MockDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.datetime.strptime("2026-01-10 01:00:00+00:00", DATE_FORMAT)

        monkeypatch.setattr("cluster_cdk.lambdas.volume_management.datetime.datetime", MockDatetime)

        MONKEYPATCH_SNAPSHOT_WARNING_DAYS = [10, 5, 3, 1]
        monkeypatch.setattr("cluster_cdk.lambdas.volume_management.SNAPSHOT_WARNING_DAYS", MONKEYPATCH_SNAPSHOT_WARNING_DAYS)
 
        snap = mock_snapshot()
        snap.tags = [
            {
                "Key": "snapshot-delete-time",
                "Value": "2026-01-30 01:00:00+00:00"
            }
        ]

        should_send = should_send_snapshot_warning_email(snap)
        assert should_send == False

    def test_should_send_snapshot_warning_email_no_warning_sent_after_first_warning_time(self, monkeypatch):
        class MockDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.datetime.strptime("2026-01-21 01:00:00+00:00", DATE_FORMAT)

        monkeypatch.setattr("cluster_cdk.lambdas.volume_management.datetime.datetime", MockDatetime)

        MONKEYPATCH_SNAPSHOT_WARNING_DAYS = [10, 5, 3, 1]
        monkeypatch.setattr("cluster_cdk.lambdas.volume_management.SNAPSHOT_WARNING_DAYS", MONKEYPATCH_SNAPSHOT_WARNING_DAYS)
 
        snap = mock_snapshot()
        snap.tags = [
            {
                "Key": "snapshot-delete-time",
                "Value": "2026-01-30 01:00:00+00:00"
            }
        ]

        should_send = should_send_snapshot_warning_email(snap)
        assert should_send == True

    def test_should_send_snapshot_warning_email_one_warning_sent_after_first_warning_time(self, monkeypatch):
        class MockDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.datetime.strptime("2026-01-22 01:00:00+00:00", DATE_FORMAT)

        monkeypatch.setattr("cluster_cdk.lambdas.volume_management.datetime.datetime", MockDatetime)

        MONKEYPATCH_SNAPSHOT_WARNING_DAYS = [10, 5, 3, 1]
        monkeypatch.setattr("cluster_cdk.lambdas.volume_management.SNAPSHOT_WARNING_DAYS", MONKEYPATCH_SNAPSHOT_WARNING_DAYS)
 
        snap = mock_snapshot()
        snap.tags = [
            {
                "Key": "snapshot-delete-time",
                "Value": "2026-01-30 01:00:00+00:00"
            },
            {
                "Key": "last-snapshot-warning-date",
                "Value": "2026-01-21 01:00:00+00:00"
            }
        ]

        should_send = should_send_snapshot_warning_email(snap)
        assert should_send == False

    def test_should_send_snapshot_warning_email_one_warning_sent_after_second_warning_time(self, monkeypatch):
        class MockDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.datetime.strptime("2026-01-26 01:00:00+00:00", DATE_FORMAT)

        monkeypatch.setattr("cluster_cdk.lambdas.volume_management.datetime.datetime", MockDatetime)

        MONKEYPATCH_SNAPSHOT_WARNING_DAYS = [10, 5, 3, 1]
        monkeypatch.setattr("cluster_cdk.lambdas.volume_management.SNAPSHOT_WARNING_DAYS", MONKEYPATCH_SNAPSHOT_WARNING_DAYS)
 
        snap = mock_snapshot()
        snap.tags = [
            {
                "Key": "snapshot-delete-time",
                "Value": "2026-01-30 01:00:00+00:00"
            },
            {
                "Key": "last-snapshot-warning-date",
                "Value": "2026-01-21 01:00:00+00:00"
            }
        ]

        should_send = should_send_snapshot_warning_email(snap)
        assert should_send == True

    def test_should_send_snapshot_warning_email_one_warning_sent_after_third_warning_time(self, monkeypatch):
        class MockDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.datetime.strptime("2026-01-29 01:00:00+00:00", DATE_FORMAT)

        monkeypatch.setattr("cluster_cdk.lambdas.volume_management.datetime.datetime", MockDatetime)

        MONKEYPATCH_SNAPSHOT_WARNING_DAYS = [10, 5, 3, 1]
        monkeypatch.setattr("cluster_cdk.lambdas.volume_management.SNAPSHOT_WARNING_DAYS", MONKEYPATCH_SNAPSHOT_WARNING_DAYS)
 
        snap = mock_snapshot()
        snap.tags = [
            {
                "Key": "snapshot-delete-time",
                "Value": "2026-01-30 01:00:00+00:00"
            },
            {
                "Key": "last-snapshot-warning-date",
                "Value": "2026-01-21 01:00:00+00:00"
            }
        ]

        should_send = should_send_snapshot_warning_email(snap)
        assert should_send == True

    def test_should_send_snapshot_warning_email_all_warning_sent_after_all_warning_time(self, monkeypatch):
        class MockDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.datetime.strptime("2026-02-10 01:00:00+00:00", DATE_FORMAT)

        monkeypatch.setattr("cluster_cdk.lambdas.volume_management.datetime.datetime", MockDatetime)

        MONKEYPATCH_SNAPSHOT_WARNING_DAYS = [10, 5, 3, 1]
        monkeypatch.setattr("cluster_cdk.lambdas.volume_management.SNAPSHOT_WARNING_DAYS", MONKEYPATCH_SNAPSHOT_WARNING_DAYS)
 
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
        assert should_send == False
