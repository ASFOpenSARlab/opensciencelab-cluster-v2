import datetime
import logging
import os
import sys
import urllib.parse

import boto3

CLAIM_TAG = "kubernetes.io/created-for/pvc/name"
CLUSTER_TAG = "KubernetesCluster"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S+00:00"

CLUSTER_NAME = os.getenv("CLUSTER_NAME", "eks-cluster-test")
SNAPSHOT_WARNING_DAYS = int(os.getenv("SNAPSHOT_WARNING_DAYS", "5"))

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger()

ec2 = boto3.client("ec2")
ec2_resource = boto3.resource("ec2")


def tags_to_dict(tags):
    if not tags:
        return {}
    return {item["Key"]: item["Value"] for item in tags}


def get_all_volumes():
    return ec2_resource.volumes.all()


def get_all_snapshots():
    this_account = boto3.client("sts").get_caller_identity().get("Account")
    return ec2_resource.snapshots.filter(
        OwnerIds=[this_account]
    )


def get_claim_user(item):
    item_tags = tags_to_dict(item.tags)
    if not item_tags.get(CLAIM_TAG, "").startswith("claim-"):
        return None
    return urllib.parse.unquote(item_tags.get(CLAIM_TAG)[6:])


def filter_users(all_items):
    """ Filter resources by claim tagged users """
    user_items = {}
    for item in all_items:
        item_tags = tags_to_dict(item.tags)

        claim_user = get_claim_user(item)
        if not claim_user:
            # Not a PVC item
            logger.debug("Skipping non-claim %s: %s", item.id, item_tags.get(CLAIM_TAG))
            continue

        if item_tags.get(CLUSTER_TAG, "") != CLUSTER_TAG:
            # Wrong Cluster
            logger.debug("Skipping cross-cluster %s: %s", item.id, item_tags.get(CLUSTER_TAG))

        # Item from a PVC in the right cluster
        user_items[claim_user] = item

    return user_items

def expiry_time(expiry):
    try:
        return datetime.datetime.strptime(expiry, DATE_FORMAT)
    except Exception as E:
        logger.error("Could not convert %s to datatime: %s", expiry, E)
        # Return a time in future since the value is garbage
        return datetime.datetime.now() + datetime.timedelta(days=100)

def is_delete_protected(item):
    if tags_to_dict(item.tags).get("do-not-delete", "") == "true":
        return True
    return False

def is_expired(item):
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

    return now > expiry_time(expiry)

def send_snapshot_warning(snapshot, claim_user):
    # Create email

    # send email to portal

    # Add reported tag
    snapshot.create_tags(
        Tags=[
            {
                "Key": "snapshot-warning-sent",
                "Value": "true"
            },
        ]
    )

    return True

def snapshot_is_expiring(snapshot):

    tags = tags_to_dict(snapshot.tags)

    # Make sure we haven't already warning
    if tags.get("snapshot-warning-sent", "") == "true":
        return False

    # date when snapshot is set to expire
    expiry = expiry_time(tags.get("snapshot-delete-time"))

    # warning trigger date
    warning_date = datetime.datetime.now() - datetime.timedelta(days=SNAPSHOT_WARNING_DAYS)

    if warning_date > expiry:
        return True

    # snapshot is not about to expire
    return False



def delete_volume(volume):
    # Create final Snapshot

    # delete pvc
    return True


def get_user_volumes():
    return filter_users(get_all_volumes())


def get_user_snapshots():
    return filter_users(get_all_snapshots())


def run_volume_management():

    for claim_user, volume in get_user_volumes().items():
        logger.info(f"VOLUME: {claim_user} | ID: {volume.id} | Size: {volume.size}GB | State: {volume.state}")
        if is_delete_protected(volume):
            logger.info(" - Volume is Delete protected!")
        elif is_expired(volume):
            logger.info(" - Volume is expired!")
            delete_volume(volume)

    for claim_user, snapshot in get_user_snapshots().items():
        logger.info(
            f"SNAPSHOT: {claim_user} | ID: {snapshot.id} | Size: {snapshot.volume_size}GB | State: {snapshot.state}"
        )

        if is_delete_protected(snapshot):
            logger.info(" - Snapshot is Delete protected!")
        elif is_expired(snapshot):
            logger.info(" - Snapshot is expired!")
            snapshot.delete(DryRun=True)
        elif snapshot_is_expiring(snapshot):
            logger.info(" - Snapshot is expiring!")
            send_snapshot_warning(snapshot, claim_user)

def lambda_hander(event, context):
    run_volume_management()

if __name__ == "__main__":
    run_volume_management()
