#!/usr/bin/env python3
import os

import boto3
import datetime

import logging

CLUSTER_NAME = os.environ["CLUSTER_NAME"]
REGION_NAME = os.environ["AWS_REGION"]
DAYS_TILL_VOLUME_DELETION = int(os.environ["DAYS_TILL_VOLUME_DELETION"])
DAYS_TILL_SNAPSHOT_DELETION = int(os.environ["DAYS_TILL_SNAPSHOT_DELETION"])

logging.basicConfig(
    format="%(asctime)s %(levelname)s (%(lineno)d) - %(message)s", level=logging.INFO
)
log = logging.getLogger(__name__)


def _get_delta_time(days: int) -> str:
    """
    Get datetime now in UTC.
    Add number of `days` until event.
    We don't need second and millisecond resolution so make those 0.
    """
    the_future_in_utc = datetime.datetime.now(
        datetime.timezone.utc
    ) + datetime.timedelta(days=days)
    return f"{the_future_in_utc.replace(second=0, microsecond=0)}"


def get_volume_for_pvc(pvc_name: str, ec2: boto3.Session.client) -> dict | None:
    """
    Get the EBS volume assigned to a pvc.

    If more than one volume is found, throw an error. If none are found, return None.
    """

    # To create a PVC we need info about any existing volumes and snapshots
    # Get any existing volume info
    vol = ec2.describe_volumes(
        Filters=[
            {
                "Name": "tag:kubernetes.io/created-for/pvc/name",
                "Values": [pvc_name],
            },
            {
                "Name": f"tag:kubernetes.io/cluster/{CLUSTER_NAME}",
                "Values": ["owned"],
            },
        ]
    )

    volumes = vol["Volumes"]
    if len(volumes) > 1:
        raise Exception(
            f"More than one volume found for pvc {pvc_name}. This should not happen."
        )
    elif len(volumes) == 0:
        log.info(f"No volumes found that matched pvc '{pvc_name}'")
        volumes = [None]

    return volumes[0]


def server_stopping_tags(pvc_name: str) -> None:
    session = boto3.Session(region_name=REGION_NAME)
    ec2 = session.client("ec2")

    log.info(f"Updating stopping tags to '{pvc_name}' in cluster '{CLUSTER_NAME}'...")

    volume = get_volume_for_pvc(pvc_name=pvc_name, ec2=ec2)

    if volume:
        ec2.create_tags(
            DryRun=False,
            Resources=[volume["VolumeId"]],
            Tags=[
                {
                    "Key": "server-stop-time",
                    "Value": _get_delta_time(days=0),
                },
                {
                    "Key": "volume-delete-time",
                    "Value": _get_delta_time(days=DAYS_TILL_VOLUME_DELETION),
                },
                {
                    "Key": "snapshot-delete-time",
                    "Value": _get_delta_time(days=DAYS_TILL_SNAPSHOT_DELETION),
                },
            ],
        )
    else:
        log.info(f"No volume found for '{pvc_name}'. Nothing to tag.")


# After stopping the notebook server, tag the volume with the current "stopping" time. This will help determine which volumes are active.
def my_post_hook(spawner: c.Spawner):  # noqa: F821
    try:
        pvc_name = spawner.pvc_name

        server_stopping_tags(pvc_name)

    except Exception as e:
        log.error("Something went wrong with the volume stopping tag post hook...")
        log.error(e)
        raise


# The variable "c" is a global variable representing the Config instance.
# This code will be appended to the end of the jupyterhub config.
# Linters like Flake8 often fail to recognize "magic" variables like "c".
# Therefore we apply "noqa: F821"
c.Spawner.post_stop_hook = my_post_hook  # noqa: F821
