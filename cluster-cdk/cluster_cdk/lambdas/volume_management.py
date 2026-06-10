import datetime
import logging
import os
import sys
import urllib.parse

import boto3

CLAIM_TAG = "kubernetes.io/created-for/pvc/name"
CLUSTER_TAG = "KubernetesCluster"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S+00:00"
REQUIRED_SNAPSHOT_TAGS = ("volume-delete-time", "snapshot-delete-time")

CLUSTER_NAME = os.getenv("CLUSTER_NAME")
SNAPSHOT_WARNING_DAYS = int(os.getenv("SNAPSHOT_WARNING_DAYS", "5"))
SNAPSHOT_EXPIRY_GRACEPERIOD = int(os.getenv("SNAPSHOT_EXPIRY_GRACEPERIOD", "1"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=logging.DEBUG if LOG_LEVEL.lower() == "debug" else logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger()

ec2 = boto3.client("ec2")
ec2_resource = boto3.resource("ec2")


def tags_to_dict(tags):
    """Convert list of dicts tags to single list"""
    if not tags:
        return {}
    return {item["Key"]: item["Value"] for item in tags}


def get_unattached_volumes():
    """Return a list of available EBS Volumes"""
    unattached_volumes = []
    for volume in ec2_resource.volumes.all():
        if volume.state == "available":
            unattached_volumes.append(volume)
        else:
            logger.debug("Ignoring attached volume %s", volume.id)
    return ec2_resource.volumes.all()


def get_all_snapshots():
    """get all volume snapshots owned by this AWS account"""
    this_account = boto3.client("sts").get_caller_identity().get("Account")
    return ec2_resource.snapshots.filter(
        OwnerIds=[this_account],
        Filters=[{"Name": "status", "Values": ["completed"]}],
    )


def get_claim_user(item):
    """Determine the username from a claim tag"""
    item_tags = tags_to_dict(item.tags)
    if not item_tags.get(CLAIM_TAG, "").startswith("claim-"):
        return None
    return urllib.parse.unquote(item_tags.get(CLAIM_TAG)[6:])


def filter_users(all_items):
    """Filter resources by claim tagged users"""
    user_items = {}
    for item in all_items:
        item_tags = tags_to_dict(item.tags)

        claim_user = get_claim_user(item)
        if not claim_user:
            # Not a PVC item
            logger.debug("Skipping non-claim %s: %s", item.id, item_tags.get(CLAIM_TAG))
            continue

        if CLUSTER_NAME and item_tags.get(CLUSTER_TAG, "") != CLUSTER_NAME:
            # Wrong Cluster
            logger.debug(
                "Skipping cross-cluster %s: %s", item.id, item_tags.get(CLUSTER_TAG)
            )
            continue

        # Item from a PVC in the right cluster
        user_items[claim_user] = item

    return user_items


def expiry_time(expiry):
    """Convert expiry time into a datetime object"""
    try:
        return datetime.datetime.strptime(expiry, DATE_FORMAT)
    except Exception as E:
        logger.error("Could not convert %s to datatime: %s", expiry, E)
        # Return a time in future since the value is garbage
        return datetime.datetime.now() + datetime.timedelta(days=100)


def is_delete_protected(item):
    """Does the item have a delete protection tag?"""
    if tags_to_dict(item.tags).get("do-not-delete", "") == "true":
        return True
    return False


def is_expired(item, grace_period_days=0):
    """Check if item is expired, with optional grace period"""
    now = datetime.datetime.now()
    tags = tags_to_dict(item.tags)

    expiry = None
    if item.id.startswith("vol-"):
        expiry = tags.get("volume-delete-time")
    elif item.id.startswith("snap-"):
        expiry = tags.get("snapshot-delete-time")

    # Could not determine the expiry time
    if not expiry:
        logger.warning("Item %s did not have delete-time tag", item.id)
        return None

    # Expire time is the marked expiry time, plus added grace period in days
    expire_time = expiry_time(expiry) + datetime.timedelta(days=grace_period_days)

    return now > expire_time


def snapshot_has_required_tags(snapshot):
    """Verify snapshot has tags required for proper management"""
    tags = tags_to_dict(snapshot.tags)

    for required_tag in REQUIRED_SNAPSHOT_TAGS:
        if not tags.get(required_tag):
            logger.warning(
                "Required tag %s not found in %s",
                required_tag,
                snapshot.id,
            )
            return False

    return True


def send_snapshot_warning(snapshot, claim_user):
    """Email the user warning of snapshot expiration"""
    # Create email

    # send email to portal

    # Add reported tag
    snapshot.create_tags(
        Tags=[
            {"Key": "snapshot-warning-sent", "Value": "true"},
        ]
    )

    return True


def send_snapshot_delete(snapshot, claim_user):
    """Send an email to the owner of a to-be-deleted snapshot"""
    tags = tags_to_dict(snapshot.tags)

    # Make sure we haven't already warning
    if tags.get("snapshot-warning-sent", "") == "true":
        return None

    # Send email

    # Add email tag
    snapshot.create_tags(
        Tags=[
            {"Key": "snapshot-delete-sent", "Value": "true"},
        ]
    )

    return True


def snapshot_is_expiring(snapshot):
    """Determine if a snapshot is inside the warning window"""
    tags = tags_to_dict(snapshot.tags)

    # Make sure we haven't already warning
    if tags.get("snapshot-warning-sent", "") == "true":
        return False

    # date when snapshot is set to expire
    expiry = expiry_time(tags.get("snapshot-delete-time"))

    # warning trigger date
    warning_date = expiry - datetime.timedelta(days=SNAPSHOT_WARNING_DAYS)

    if datetime.datetime.now() > warning_date:
        return True

    # snapshot is not about to expire
    return False


def get_snapshot_for_volume(volume, user_snapshots):
    """Check if a specific volume has a snapshot available"""
    for claim_user, snapshot in user_snapshots.items():
        if snapshot.volume_id == volume.volume_id:
            logger.info(
                "Found Snapshot %s for Volume %s for user %s",
                snapshot.id,
                volume.volume_id,
                claim_user,
            )
            return snapshot
        else:
            logger.debug(
                "Snapshot %s is for %s, not %s",
                snapshot.id,
                snapshot.volume_id,
                volume.volume_id,
            )

    return None


def delete_volume(volume):
    """Delete a user's volume by removing their PVC in K8s"""
    # Create final Snapshot

    # delete pvc
    return True


def get_user_volumes():
    """Return unattached user volumes for a cluster"""
    return filter_users(get_unattached_volumes())


def get_user_snapshots():
    """Return user snapshots for a cluster"""
    return filter_users(get_all_snapshots())


def run_volume_management():
    """Process Volumes and Snapshots"""
    user_volumes = get_user_volumes()
    user_snapshots = get_user_snapshots()

    for claim_user, volume in user_volumes.items():
        logger.info(
            f"VOLUME: {claim_user} | ID: {volume.id} | Size: {volume.size}GB | State: {volume.state}"
        )

        # attempt to find a snapshot for the volume
        snapshot_from_volume = get_snapshot_for_volume(volume, user_snapshots)

        if is_delete_protected(volume):
            logger.info(" - Volume is Delete protected!")
        elif not snapshot_from_volume:
            logger.error(" - Volume has no active snapshot!")
        elif not snapshot_has_required_tags(snapshot_from_volume):
            logger.error(" - Ignoring volume with invalid snapshot tags")
        elif is_expired(volume):
            logger.info(" - Volume is expired!")
            delete_volume(volume)

    for claim_user, snapshot in user_snapshots.items():
        logger.info(
            f"SNAPSHOT: {claim_user} | ID: {snapshot.id} | Size: {snapshot.volume_size}GB | State: {snapshot.state}"
        )

        if is_delete_protected(snapshot):
            logger.info(" - Snapshot is Delete protected!")
        elif is_expired(snapshot, grace_period_days=SNAPSHOT_EXPIRY_GRACEPERIOD):
            logger.info(" - Snapshot is expired past grace period!")
            snapshot.delete(DryRun=True)
        elif is_expired(snapshot):
            logger.info(" - Snapshot is in expired grace period!")
            send_snapshot_delete(snapshot, claim_user)
        elif snapshot_is_expiring(snapshot):
            logger.info(" - Snapshot is expiring!")
            send_snapshot_warning(snapshot, claim_user)


def lambda_hander(event, context):
    run_volume_management()


if __name__ == "__main__":
    run_volume_management()
