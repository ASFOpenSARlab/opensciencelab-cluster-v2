import datetime
import json
import logging
import os
import subprocess
import sys
import traceback

import boto3
import escapism
import jinja2
from kubernetes import client as k8s_client, config as k8s_config
import requests
from botocore.exceptions import ClientError
from opensarlab.auth import encryptedjwt

CLAIM_TAG = "kubernetes.io/created-for/pvc/name"
CLUSTER_TAG = "KubernetesCluster"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S%z"
REQUIRED_SNAPSHOT_TAGS = ("volume-delete-time", "snapshot-delete-time")

LAB_SHORT_NAME = os.getenv("LAB_SHORT_NAME", "UNKNOWN")
CLUSTER_NAME = os.getenv("CLUSTER_NAME", LAB_SHORT_NAME)
# Convert SNAPSHOT_WARNING_DAYS string to reverse sorted list of ints
SNAPSHOT_WARNING_DAYS: list[int] = sorted(
    list({int(num) for num in os.getenv("SNAPSHOT_WARNING_DAYS", "5").split(",")}),
    reverse=True,
)
SNAPSHOT_GRACEPERIOD_DAYS = float(os.getenv("SNAPSHOT_GRACEPERIOD_DAYS", "1.0"))
SNS_ALERT_TOPIC_ARN = os.getenv("ALERT_SNS_TOPIC_ARN")
PORTAL_DOMAIN = os.getenv("PORTAL_DOMAINS", "").split(",")[0].strip()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
AWS_CLI_PATH = os.getenv("AWS_CLI_PATH", "/opt/awscli/aws")
KUBECONFIG = os.getenv("KUBECONFIG", "/tmp/eks.conf")
logging.basicConfig(
    stream=sys.stdout,
)
logger = logging.getLogger()
logger.setLevel(logging.DEBUG if LOG_LEVEL.lower() == "debug" else logging.INFO)

# Email sending parameters
absolute_path = os.path.abspath(__file__)
current_directory = os.path.dirname(absolute_path)
JINJA_LOADER = jinja2.Environment(
    loader=jinja2.FileSystemLoader(f"{current_directory}/templates/"),
    autoescape=jinja2.select_autoescape(),
    undefined=jinja2.StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)

SSO_SECRET = None
CONCERNING_ISSUES = []


ec2_client = None
ec2_resource = None


def get_ec2_client():
    global ec2_client
    if not ec2_client:
        ec2_client = boto3.client("ec2")
    return ec2_client


def get_ec2_resource():
    global ec2_resource
    if not ec2_resource:
        ec2_resource = boto3.resource("ec2")
    return ec2_resource


def set_sso_secret():
    """Grab the SSO secret for sending emails. Die if this fails. No Exception Handling"""
    global SSO_SECRET
    secret_arn = os.getenv("SSO_SECRET_ARN")
    ssm_client = boto3.client("secretsmanager")
    SSO_SECRET = ssm_client.get_secret_value(SecretId=secret_arn)["SecretString"]


def reset_concerning_issues():
    """Reset issues between lambda runs"""
    global CONCERNING_ISSUES
    CONCERNING_ISSUES = []


def add_concerning_issue(**args):
    """Keep a list of concerning issues to email to admins"""
    global CONCERNING_ISSUES

    # Prevent duplicates
    if args in CONCERNING_ISSUES:
        return False

    # Record
    logger.warning(args["message"])
    CONCERNING_ISSUES.append(args)
    return True


def email_concerning_issues():
    if not CONCERNING_ISSUES:
        return True

    email_template = JINJA_LOADER.get_template("error_report_email.j2")
    email_template_params = {
        "cluster_name": CLUSTER_NAME,
        "issues": CONCERNING_ISSUES,
    }
    email_payload = {
        "to": {"username": "osl-admin"},
        "from": {"username": "osl-admin"},
        "subject": "OpenScienceLab Storage Management Errors",
        "html_body": email_template.render(email_template_params),
    }

    # send email to portal
    try:
        send_email_to_portal(email_payload)
        return True
    except requests.exceptions.RequestException:
        logger.exception("There was a problem sending error email, using SNS")

    # If we couldn't send formatted email, try via SNS
    exception_message = (
        "Could not send concerning issues email via Portal:\n\n"
        f"{json.dumps(CONCERNING_ISSUES, default=str, indent=2)}"
    )
    alert_fatal_exception(exception_message)


def tags_to_dict(tags):
    """Convert list of dicts tags to single list"""
    if not tags:
        return {}
    return {item["Key"]: item["Value"] for item in tags}


def get_all_unattached_volumes_in_lab():
    """Return a list of available EBS Volumes, sorted from oldest to newest"""
    ec2_resource = get_ec2_resource()
    unattached_volumes = ec2_resource.volumes.filter(
        Filters=[
            {"Name": "status", "Values": ["available"]},
            {"Name": f"tag:{CLUSTER_TAG}", "Values": [CLUSTER_NAME]},
        ]
    )
    return sorted(unattached_volumes, key=lambda v: v.create_time)


def get_all_completed_snapshots_in_lab() -> list:
    """Return a list of EBS snapshots owned by this AWS account, sorted from oldest to newest"""
    this_account = boto3.client("sts").get_caller_identity().get("Account")
    ec2_resource = get_ec2_resource()
    snapshots = ec2_resource.snapshots.filter(
        OwnerIds=[this_account],
        Filters=[
            {"Name": "status", "Values": ["completed"]},
            {"Name": f"tag:{CLUSTER_TAG}", "Values": [CLUSTER_NAME]},
        ],
    )
    return sorted(snapshots, key=lambda s: s.start_time)


def delete_older_duplicates(snapshots: list) -> list:
    """
    Sometimes there might be an older duplicate of a snapshot. This could occur due to lifecyle managament
    abandoning a snapshot due to the deletion of it's original volume.

    Sort by pvc name. Delete the older snapshots if there are more than one. There should always be at least one snapshot
    present. The code will handle the one remaining snapshot as appropriate.

    Ignore if the `do-not-delete` tag is present. Since the hub db might have a duplicate volume, ignore `hub-db-dir`.

    return: list of snapshots with duplicates removed

    """
    reduced_snapshots = []
    hash_table = {}

    for snapshot in snapshots:
        # Create a hash table with the pvc name (which is presumed to be unique) as the key.
        # If subsequent entries match the hash key, there are duplicates.
        claim_name = get_claim_name(snapshot)
        start_time: datetime.datetime = snapshot.start_time

        if (
            claim_name == "hub-db-dir"
            or not start_time
            or is_delete_protected(snapshot)
        ):
            continue

        hash_key = str(abs(hash(claim_name)))
        hash_value = {"snapshot": snapshot, "start_time": start_time}

        a = hash_table.get(hash_key, [])
        a.append(hash_value)
        hash_table[hash_key] = a

    for hash_value in hash_table.values():
        # Sort by start time, save the latest (or do-not-delete), delete the rest
        hash_value = sorted(hash_value, key=lambda e: e["start_time"], reverse=True)
        for i, value in enumerate(hash_value):
            if i == 0:
                reduced_snapshots.append(value["snapshot"])
            else:
                value["snapshot"].delete()

    return reduced_snapshots


def get_claim_name(item):
    """Determine the username from a claim tag"""
    item_tags = tags_to_dict(item.tags)
    claim_name = item_tags.get(CLAIM_TAG, "")
    if not claim_name.startswith("claim-"):
        return None
    return claim_name


def get_eks_client():
    """use awscli to generate a KUBECONFIG for the cluster"""
    # Hacky way to set up kubectl
    result = subprocess.run(
        [
            AWS_CLI_PATH,
            "eks",
            "update-kubeconfig",
            "--name",
            CLUSTER_NAME,
            "--kubeconfig",
            KUBECONFIG,
            "--alias",
            "eks",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        add_concerning_issue(
            message=f"Could not generate KUBECONF file: {result.stdout}",
        )

    # Uhhggg.. Stupid hack because you can't change the aws path in kubeconfig file
    # https://stackoverflow.com/a/71222634/21674565
    with open(KUBECONFIG, "r") as file:
        content = file.read()
    content = content.replace("command: aws", f"command: {AWS_CLI_PATH}")
    with open(KUBECONFIG, "w") as file:
        file.write(content)

    if result.returncode != 0:
        add_concerning_issue(
            message=f"Could not generate KUBECONF file: {result.stdout}",
        )

    # Read kubeconfig file
    k8s_config.load_kube_config(config_file=KUBECONFIG)
    return k8s_client.CoreV1Api()


def delete_pvc(
    claim_name: str, volume_id: str, kube_client: k8s_client.CoreV1Api
) -> None:
    """
    Delete a user's volume by removing their PVC in K8s.
    If the PVC doesn't exist, delete volume directly.
    """
    # Attempt to remove PVC
    try:
        kube_client.delete_namespaced_persistent_volume_claim(
            name=claim_name,
            namespace="jupyter",
        )
    except k8s_client.rest.ApiException:
        logger.warning(
            f"User claim {claim_name} can not be deleted in {CLUSTER_NAME}. Deleting volume '{volume_id}' directly..."
        )
        try:
            ec2_resource = get_ec2_resource()
            volume = ec2_resource.Volume(volume_id)
            volume.delete()
            logger.info(f"Volume {volume_id} deleted in {CLUSTER_NAME}")
        except ClientError as e:
            exception_message = f"Error deleting volume {volume_id} in {CLUSTER_NAME}: {e.response['Error']['Message']}"
            add_concerning_issue(message=exception_message, user=claim_name)
            logger.exception(exception_message)


def filter_by_user(all_items: list) -> dict:
    """Filter resources by claim tagged users. Assume one volume/snapshot per person."""
    user_items = {}
    for item in all_items:
        item_tags = tags_to_dict(item.tags)

        claim_name = get_claim_name(item)
        if not claim_name:
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
        user_items[claim_name] = item

    return user_items


def expiry_time(expiry):
    """Convert expiry time into a datetime object"""
    try:
        return datetime.datetime.strptime(expiry, DATE_FORMAT)
    except Exception as E:
        logger.error("Could not convert %s to datatime: %s", expiry, E)
        # Return a time in future since the value is garbage
        return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            days=100
        )


def is_delete_protected(item) -> bool:
    """Does the item have a delete protection tag?"""
    if tags_to_dict(item.tags).get("do-not-delete", "") == "true":
        return True
    return False


def is_expired(item, grace_period_days=0):
    """Check if item is expired, with optional grace period"""
    now = datetime.datetime.now(datetime.timezone.utc)
    tags = tags_to_dict(item.tags)

    expiry = None
    if item.id.startswith("vol-"):
        expiry = tags.get("volume-delete-time")
    elif item.id.startswith("snap-"):
        expiry = tags.get("snapshot-delete-time")

    # Could not determine the expiry time
    if not expiry:
        add_concerning_issue(
            message=f"Item {item.id} did not have delete-time tag",
        )
        return None

    # Expire time is the marked expiry time, plus added grace period in days
    expire_time = expiry_time(expiry) + datetime.timedelta(days=grace_period_days)

    logger.debug(f" - Now datetime: {now} Expiration datetime: {expire_time}")

    return now > expire_time


def snapshot_has_required_tags(snapshot):
    """Verify snapshot has tags required for proper management"""
    tags = tags_to_dict(snapshot.tags)

    for required_tag in REQUIRED_SNAPSHOT_TAGS:
        if not tags.get(required_tag):
            logger.warning(f"Required tag {required_tag} not found in {snapshot.id}")
            return False

    return True


def get_unescaped_user(claim_name: str) -> str:
    """Unescape claim name to get actual username"""
    unescaped_username = claim_name.removeprefix("claim-")
    return escapism.unescape(unescaped_username, escape_char="-")


def send_snapshot_warning(snapshot, claim_name):
    """Email the user warning of snapshot expiration"""
    # Delete Time:
    tags = tags_to_dict(snapshot.tags)
    expiry = expiry_time(tags.get("snapshot-delete-time"))
    expiry_string = expiry.strftime("%Y-%m-%d %H:%M:%S UTC")

    unescaped_user = get_unescaped_user(claim_name)

    # Create email
    email_template = JINJA_LOADER.get_template("snapshot_warning_email.j2")
    email_template_params = {
        "username": unescaped_user,
        "lab_short_name": LAB_SHORT_NAME,
        "volume_delete_time": expiry_string,
        "portal_domain_name": PORTAL_DOMAIN,
    }
    email_payload = {
        "to": {"username": unescaped_user},
        "from": {"username": "osl-admin"},
        "cc": {"username": "osl-admin"},
        "subject": "OpenScienceLab Notification - Storage Warning",
        "html_body": email_template.render(email_template_params),
    }

    # send email to portal
    send_email_to_portal(email_payload)

    # Update last warning tag
    snapshot.create_tags(
        Tags=[
            {
                "Key": "last-snapshot-warning-date",
                "Value": datetime.datetime.now(datetime.timezone.utc).strftime(
                    DATE_FORMAT
                ),
            },
        ]
    )

    return True


def send_snapshot_delete(snapshot, claim_name):
    """Send email to the owner of a to-be-deleted snapshot"""
    tags = tags_to_dict(snapshot.tags)

    # Make sure we haven't already sent delete email
    if tags.get("snapshot-delete-sent", "") == "true":
        logger.info(" - Deletion email sent previously")
        return None

    unescaped_user = get_unescaped_user(claim_name)

    # Create email
    email_template = JINJA_LOADER.get_template("volume_delete_email.j2")
    email_template_params = {
        "username": unescaped_user,
        "lab_short_name": LAB_SHORT_NAME,
    }
    email_payload = {
        "to": {"username": unescaped_user},
        "from": {"username": "osl-admin"},
        "cc": {"username": "osl-admin"},
        "subject": "OpenScienceLab Notification - Storage Deleted",
        "html_body": email_template.render(email_template_params),
    }

    # send email to portal
    send_email_to_portal(email_payload)
    logger.info(" - Deletion email sent")

    # Add email tag
    snapshot.create_tags(
        Tags=[
            {"Key": "snapshot-delete-sent", "Value": "true"},
        ]
    )

    return True


def should_send_snapshot_warning_email(snapshot):
    """Determine if a snapshot is inside the warning window"""
    tags = tags_to_dict(snapshot.tags)

    # date when snapshot is set to expire
    expiry = expiry_time(tags.get("snapshot-delete-time"))

    # All datetimes a warning email should be sent
    warning_dates = [
        expiry - datetime.timedelta(days=day) for day in SNAPSHOT_WARNING_DAYS
    ]

    # Last datetime a warning email was sent
    # Defaults to January 1, 1970, at 00:00:00 UTC
    last_warning_date = datetime.datetime.strptime(
        tags.get(
            "last-snapshot-warning-date",
            datetime.datetime.fromtimestamp(0, datetime.timezone.utc).strftime(
                DATE_FORMAT
            ),
        ),
        DATE_FORMAT,
    )

    # Get next datetime a warning email should be sent out, None if there are no more emails to send
    next_warning_date = None
    for date in warning_dates:
        if last_warning_date < date:
            next_warning_date = date
            break

    logger.debug(f" - All warning datetimes: {warning_dates}")
    logger.debug(f" - Last warning datetime: {last_warning_date}")
    logger.debug(f" - Next warning datetime: {next_warning_date}")

    # Send email if
    # * there is another email to be sent
    # * it is currently after when the next warning should be sent
    if (
        next_warning_date
        and datetime.datetime.now(datetime.timezone.utc) > next_warning_date
    ):
        return True
    return False


def get_snapshot_for_volume(volume, user_snapshots):
    """Check if a specific volume has a snapshot available"""
    for claim_name, snapshot in user_snapshots.items():
        if snapshot.volume_id == volume.volume_id:
            logger.info(
                "Found Snapshot %s for Volume %s for user %s",
                snapshot.id,
                volume.volume_id,
                claim_name,
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


def get_user_volumes():
    """Return unattached user volumes for a cluster"""
    return filter_by_user(get_all_unattached_volumes_in_lab())


def get_user_snapshots():
    """Return user snapshots for a cluster"""
    return filter_by_user(delete_older_duplicates(get_all_completed_snapshots_in_lab()))


def send_email_to_portal(email_payload):
    """Proxy an email through portal endpoint"""
    encrypted_data = encryptedjwt.encrypt(email_payload, sso_token=SSO_SECRET)
    portal_email_url = f"https://{PORTAL_DOMAIN}/portal/hub/user/email"

    # Send Request
    response = requests.post(url=portal_email_url, data=encrypted_data, timeout=15)
    logger.info(
        f"Sent '{email_payload['subject']}' to '{email_payload['to']}' via "
        f"'{portal_email_url}' with return status of {response.status_code}"
    )

    # Raise an exception on failure
    response.raise_for_status()


def run_volume_management():
    """Process Volumes and Snapshots"""

    # Verify we have the SSO Secret or die
    set_sso_secret()

    # Loop up resources
    logger.info("Setting up EKS Client for %s", CLUSTER_NAME)
    kube_client = get_eks_client()

    logger.info("Querying for Volumes...")
    user_volumes: dict = get_user_volumes()
    logger.info("Found %s user volumes", len(user_volumes))

    logger.info("Querying for Snapshots...")
    user_snapshots: dict = get_user_snapshots()
    logger.info("Found %s user snapshots", len(user_snapshots))

    for claim_name, volume in user_volumes.items():
        logger.info(
            f"VOLUME: {claim_name} | ID: {volume.id} | Size: {volume.size}GB | State: {volume.state}"
        )

        # attempt to find a snapshot for the volume
        snapshot_from_volume = get_snapshot_for_volume(volume, user_snapshots)

        if is_delete_protected(volume):
            logger.info(" - Volume is Delete protected!")
        elif not snapshot_from_volume:
            logger.warning(" - Volume has no active snapshot")
        elif not snapshot_has_required_tags(snapshot_from_volume):
            logger.error(" - Ignoring volume with invalid snapshot tags")
        elif is_expired(volume):
            logger.info(" - Volume is expired!")
            delete_pvc(claim_name, volume.id, kube_client)

    for claim_name, snapshot in user_snapshots.items():
        logger.info(
            f"SNAPSHOT: {claim_name} | ID: {snapshot.id} | Size: {snapshot.volume_size}GB | State: {snapshot.state}"
        )

        if not snapshot_has_required_tags(snapshot):
            logger.warning(" - Snapshot is missing tags!")
        elif is_delete_protected(snapshot):
            logger.info(" - Snapshot is Delete protected!")
        elif is_expired(snapshot, grace_period_days=SNAPSHOT_GRACEPERIOD_DAYS):
            logger.info(" - Deleting Snapshot")
            snapshot.delete()
        elif is_expired(snapshot):
            logger.info(" - Snapshot is in grace period!")
            send_snapshot_delete(snapshot, claim_name)
        elif should_send_snapshot_warning_email(snapshot):
            logger.info(" - Sending a snapshot warning email!")
            send_snapshot_warning(snapshot, claim_name)


def alert_fatal_exception(exception_message):
    """If SNS topic is provided, send uncaught fatal exception to SNS"""
    if SNS_ALERT_TOPIC_ARN:
        sns_client = boto3.client("sns")
        sns_client.publish(
            TopicArn=SNS_ALERT_TOPIC_ARN,
            Message=exception_message,
            Subject=f"Exception alert from {CLUSTER_NAME}",
        )
    else:
        add_concerning_issue(message="No SNS topic configured")


def lambda_handler(_event, _context):
    try:
        reset_concerning_issues()
        run_volume_management()
    except Exception as E:
        alert_fatal_exception(traceback.format_exc())
        add_concerning_issue(message=f"Uncaught Exception: {E}")
        logger.exception("Uncaught Exception:")

    # This should try to run even on uncaught exception above
    try:
        email_concerning_issues()
    except Exception:
        alert_fatal_exception(traceback.format_exc())
        logger.exception("Uncaught Exception:")

    return {"statusCode": 200, "body": "Storage management successfull!"}


if __name__ == "__main__":
    lambda_handler("event", "context")
