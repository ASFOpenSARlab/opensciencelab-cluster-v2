import datetime
import json
import logging
import os
import subprocess
import sys
import traceback
import urllib.parse

import boto3
import jinja2
import kubernetes
import requests

from opensarlab.auth import encryptedjwt

CLAIM_TAG = "kubernetes.io/created-for/pvc/name"
CLUSTER_TAG = "KubernetesCluster"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S+00:00"
REQUIRED_SNAPSHOT_TAGS = ("volume-delete-time", "snapshot-delete-time")

CLUSTER_NAME = os.getenv("CLUSTER_NAME")
LAB_SHORT_NAME = os.getenv("LAB_SHORT_NAME", "CLUSTER_NAME")
SNAPSHOT_WARNING_DAYS: list[int] = list(set([int(num) for num in os.getenv("SNAPSHOT_WARNING_DAYS", "5").split(",")]))
SNAPSHOT_EXPIRY_GRACEPERIOD = int(os.getenv("SNAPSHOT_EXPIRY_GRACEPERIOD", "1"))
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


def get_unattached_volumes():
    """Return a list of available EBS Volumes"""
    unattached_volumes = []
    ec2_resource = get_ec2_resource()
    for volume in ec2_resource.volumes.all():
        if volume.state == "available":
            unattached_volumes.append(volume)
        else:
            logger.debug("Ignoring attached volume %s", volume.id)
    return ec2_resource.volumes.all()


def get_all_snapshots():
    """get all volume snapshots owned by this AWS account"""
    this_account = boto3.client("sts").get_caller_identity().get("Account")
    ec2_resource = get_ec2_resource()
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
    kubernetes.config.load_kube_config(config_file=KUBECONFIG)
    return kubernetes.client.CoreV1Api()


def get_all_pvcs(kube_client):
    """Return a list of all PVC's in the jupyter namespace of the cluster"""
    all_pvcs = kube_client.list_namespaced_persistent_volume_claim(namespace="jupyter")
    return [
        pvc.metadata.name for pvc in all_pvcs.items if pvc.metadata.name != "hub-db-dir"
    ]


def delete_pvc(claim_user, all_pvcs, kube_client):
    """Delete a user's volume by removing their PVC in K8s"""
    user_claim_id = f"claim-{claim_user}"

    # Verify the PVC exists
    if user_claim_id not in all_pvcs:
        add_concerning_issue(
            user=user_claim_id,
            message=f"user pvc {user_claim_id} does not exist in {CLUSTER_NAME}",
        )
        return False

    # Attempt to remove PVC
    try:
        kube_client.delete_namespaced_persistent_volume_claim(
            name=user_claim_id,
            namespace="jupyter",
        )
    except kubernetes.client.rest.ApiException:
        exception_message = f"Could not delete PVC {user_claim_id} in {CLUSTER_NAME}"
        add_concerning_issue(message=exception_message, user=claim_user)
        logger.exception(exception_message)
        return True

    # PVC Successfully deleted
    return True


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
        add_concerning_issue(
            message=f"Item {item.id} did not have delete-time tag",
        )
        return None

    # Expire time is the marked expiry time, plus added grace period in days
    expire_time = expiry_time(expiry) + datetime.timedelta(days=grace_period_days)

    return now > expire_time


def snapshot_has_required_tags(snapshot):
    """Verify snapshot has tags required for proper management"""
    tags = tags_to_dict(snapshot.tags)

    for required_tag in REQUIRED_SNAPSHOT_TAGS:
        if not tags.get(required_tag):
            add_concerning_issue(
                message=f"Required tag {required_tag} not found in {snapshot.id}",
                snapshot=snapshot.id,
            )
            return False

    return True


def send_snapshot_warning(snapshot, claim_user):
    """Email the user warning of snapshot expiration"""
    # Delete Time:
    tags = tags_to_dict(snapshot.tags)
    expiry = expiry_time(tags.get("snapshot-delete-time"))
    expiry_string = expiry.strftime("%Y-%m-%d %H:%M:%S UTC")

    # Create email
    email_template = JINJA_LOADER.get_template("snapshot_warning_email.j2")
    email_template_params = {
        "username": claim_user,
        "lab_short_name": LAB_SHORT_NAME,
        "volume_delete_time": expiry_string,
        "portal_domain_name": PORTAL_DOMAIN,
    }
    email_payload = {
        "to": {"username": claim_user},
        "from": {"username": "osl-admin"},
        "cc": {"username": "osl-admin"},
        "subject": "OpenScienceLab Notification - Storage Warning",
        "html_body": email_template.render(email_template_params),
    }

    # send email to portal
    send_email_to_portal(email_payload)

    # Add reported tag
    snapshot.create_tags(
        Tags=[
            {"Key": "last-snapshot-warning-date", "Value": datetime.datetime.now().strftime(DATE_FORMAT)},
        ]
    )

    return True


def send_snapshot_delete(snapshot, claim_user):
    """Send email to the owner of a to-be-deleted snapshot"""
    tags = tags_to_dict(snapshot.tags)

    # Make sure we haven't already warning
    if tags.get("snapshot-warning-sent", "") == "true":
        return None

    # Create email
    email_template = JINJA_LOADER.get_template("volume_delete_email.j2")
    email_template_params = {
        "username": claim_user,
        "lab_short_name": LAB_SHORT_NAME,
    }
    email_payload = {
        "to": {"username": claim_user},
        "from": {"username": "osl-admin"},
        "cc": {"username": "osl-admin"},
        "subject": "OpenScienceLab Notification - Storage Deleted",
        "html_body": email_template.render(email_template_params),
    }

    # send email to portal
    send_email_to_portal(email_payload)

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
    warning_dates = [expiry - datetime.timedelta(days=day) for day in SNAPSHOT_WARNING_DAYS]

    # Last datetime a warning email was sent
    last_warning_date = datetime.datetime.strptime(tags.get("last-snapshot-warning-date", datetime.datetime.fromtimestamp(0).strftime(DATE_FORMAT)), DATE_FORMAT)

    # Get next datetime a warning email should be sent out, None if there are no more emails to send
    next_warning_date = None
    for date in warning_dates:
        if last_warning_date < date:
            next_warning_date = date
            break

    # Send email if
    # * there is another email to be sent
    # * it is currently after when the next warning should be sent
    if next_warning_date and datetime.datetime.now() > next_warning_date:
        return True
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


def get_user_volumes():
    """Return unattached user volumes for a cluster"""
    return filter_users(get_unattached_volumes())


def get_user_snapshots():
    """Return user snapshots for a cluster"""
    return filter_users(get_all_snapshots())


def send_email_to_portal(email_payload):
    """Proxy an email through portal endpoint"""
    encrypted_data = encryptedjwt.encrypt(email_payload, sso_token=SSO_SECRET)
    portal_email_url = f"{PORTAL_DOMAIN}/portal/hub/user/email"

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
    user_volumes = get_user_volumes()
    logger.info("Found %s user volumes", len(user_volumes))

    logger.info("Querying for Snapshots...")
    user_snapshots = get_user_snapshots()
    logger.info("Found %s user snapshots", len(user_snapshots))

    logger.info("Querying for PVCs...")
    all_pvcs = get_all_pvcs(kube_client)
    logger.info("Found %s user PVCs", len(all_pvcs))

    for claim_user, volume in user_volumes.items():
        logger.info(
            f"VOLUME: {claim_user} | ID: {volume.id} | Size: {volume.size}GB | State: {volume.state}"
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
            if not delete_pvc(claim_user, all_pvcs, kube_client):
                logger.error(" - There was a problem removing PVC for %s", volume.id)

    for claim_user, snapshot in user_snapshots.items():
        logger.info(
            f"SNAPSHOT: {claim_user} | ID: {snapshot.id} | Size: {snapshot.volume_size}GB | State: {snapshot.state}"
        )

        if not snapshot_has_required_tags(snapshot):
            logger.warning(" - Snapshot is missing tags!")
        elif is_delete_protected(snapshot):
            logger.info(" - Snapshot is Delete protected!")
        elif is_expired(snapshot, grace_period_days=SNAPSHOT_EXPIRY_GRACEPERIOD):
            logger.info(" - Snapshot is expired past grace period!")
            snapshot.delete()
        elif is_expired(snapshot):
            logger.info(" - Snapshot is in expired grace period!")
            send_snapshot_delete(snapshot, claim_user)
        elif should_send_snapshot_warning_email(snapshot):
            logger.info(" - Snapshot is expiring!")
            send_snapshot_warning(snapshot, claim_user)


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


if __name__ == "__main__":
    lambda_handler("event", "context")
