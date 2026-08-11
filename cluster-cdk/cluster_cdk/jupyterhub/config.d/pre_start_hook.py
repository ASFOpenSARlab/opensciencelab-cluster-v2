#!/usr/bin/env python3
import os
import datetime
import logging
import traceback
import re

import boto3

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException

LAB_SHORT_NAME = os.environ["LAB_SHORT_NAME"]
CLUSTER_NAME = os.environ["CLUSTER_NAME"]
REGION_NAME = os.environ["AWS_REGION"]
AZ_NAME = os.environ["AZ_NAME"]
COST_TAG_KEY = os.environ["COST_TAG_KEY"]
COST_TAG_VALUE = os.environ["COST_TAG_VALUE"]
NAMESPACE = os.environ["K8s_NAMESPACE"]


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


def standarized_storage_capacity(storage_capacity: str) -> int:
    """
    Args:
        storage_capacity: volume size string. Ex. "100Gi", "2Ti"

    Returns:
        An integer of GiB size.
    """
    # Convert the desired volume size into a standarized value
    alpha = " ".join(re.findall("[a-zA-Z]+", storage_capacity)).lower()
    number = int(" ".join(re.findall("[0-9]+", storage_capacity)))

    possible_units = {
        "ti": 2**40,
        "gi": 2**30,
        "t": 10**12,
        "g": 10**9,
        "": 1,  # Default without units is bytes
    }

    vol_size = number * possible_units[alpha]

    # Volume needs to be in GiB (an int without the label)
    vol_size = int(vol_size * 2**-30)
    if vol_size < 0:
        vol_size = 1

    return vol_size


def expand_volume(volume_size: str, pvc_name: str, api: k8s_client.CoreV1Api) -> None:
    vol_size: int = standarized_storage_capacity(volume_size)

    body = {"spec": {"resources": {"requests": {"storage": f"{vol_size}Gi"}}}}

    api.patch_namespaced_persistent_volume_claim(
        name=pvc_name, namespace=NAMESPACE, body=body
    )
    log.info(f"Successfully patched PVC {pvc_name} storage parameter to {vol_size}Gi.")


def get_tag_value(resource, key):
    val = [s["Value"] for s in resource["Tags"] if s["Key"] == key]

    if not val:
        val = [""]

    return str(val[0])


def get_volume_for_pvc(pvc_name: str, ec2: boto3.Session.client) -> dict | None:
    """
    Get the EBS volume assigned to a pvc.

    If more than one volume is found, throw and error. If none are found, return None.
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


def get_snapshot_for_pvc(pvc_name: str, ec2: boto3.Session.client) -> dict | None:
    """
    Get the EBS snapshot assigned to a pvc.

    If more than one snapshot is found, use the latest one. If none are found, return None.
    """

    # Does the user have any snapshots?
    snap = ec2.describe_snapshots(
        Filters=[
            {
                "Name": "tag:kubernetes.io/created-for/pvc/name",
                "Values": [pvc_name],
            },
            {
                "Name": f"tag:kubernetes.io/cluster/{CLUSTER_NAME}",
                "Values": ["owned"],
            },
            {"Name": "status", "Values": ["completed"]},
        ],
        OwnerIds=["self"],
    )
    snap = snap["Snapshots"]

    if len(snap) > 1:
        snap = sorted(snap, key=lambda s: s["StartTime"], reverse=True)
        log.warning(
            f"\nWARNING ***** More than one snapshot found for pvc: {pvc_name}. Claiming the latest one: \n{snap[0]}."
        )
    elif len(snap) == 0:
        log.info(f"No snapshot found that matched pvc '{pvc_name}'")
        snap = [None]

    return snap[0]


def get_user_volume(
    username: str, pvc_name: str, storage_capacity: str, annotations: dict, labels: dict
) -> None:
    """
    # Before mounting the home directory, check to see if a PVC exists.
    # If it does, then assume there is a EBS volume associated.
    # If it doesn't, check for any EBS snapshots.
    # If a snapshot exists, create a volume from the snapshot.
    # Else, JupyterHub will create the volume.
    """

    k8s_config.load_incluster_config()
    api = k8s_client.CoreV1Api()

    desired_vol_size = standarized_storage_capacity(storage_capacity)

    session = boto3.Session(region_name=REGION_NAME)
    ec2 = session.client("ec2")

    pvcs = api.list_namespaced_persistent_volume_claim(namespace=NAMESPACE, watch=False)

    # Check to see if an pvc already exists
    has_pvc = pvc_name in [item.metadata.name for item in pvcs.items]

    volume = get_volume_for_pvc(pvc_name=pvc_name, ec2=ec2)

    snapshot = get_snapshot_for_pvc(pvc_name=pvc_name, ec2=ec2)

    # Case 1: PVC does exist with an existing volume. Do nothing except potentially expand volume size.
    if volume and has_pvc:
        log.info(
            f"PVC '{pvc_name}' exists! Therefore a volume should have already been assigned to user '{username}'."
        )

        # Boto3 describe_volume returns an integer of volume size in GiBs
        vol_size: int = volume["Size"]

        # Since we only really work in GB sized increments, we can compare the volumes on the Gi scale.
        if desired_vol_size > vol_size:
            log.info(
                f"Expanding existing volume size from {vol_size}Gi to {desired_vol_size}Gi"
            )
            expand_volume(f"{desired_vol_size}Gi", pvc_name, api)

        return

    # Case 2: PVC does exist. There is no existing volume. Delete PVC (and PV) and move on to Case 3 or Case 4.
    if not volume and has_pvc:
        log.warning(
            "No volume found to match existing PVC. Was the volume deleted in the AWS console? Deleting the PVC (and PV)."
        )

        # https://github.com/kubernetes-client/python/blob/master/kubernetes/docs/CoreV1Api.md#delete_namespaced_persistent_volume_claim
        api.delete_namespaced_persistent_volume_claim(
            name=pvc_name, namespace=NAMESPACE, grace_period_seconds=0
        )

        has_pvc = False

    vol_id = None

    # Case 3: No PVC but existing volume. Any existing snapshots are ignored. A PVC and PV will be created later
    # Volume will not be expanded if desired since the PVC doesn't exist.
    # Any subsequent runs will expand to the profile's storage value. While this might cause some delayed expansion, this case is rare enough not to matter.
    if volume and not has_pvc:
        log.warning(
            f"Volume found for '{username}' without pvc '{pvc_name}'. This is unusual."
        )

        vol_id = volume["VolumeId"]
        desired_vol_size = volume["Size"]

    # Case 4: No PVC, no existing volume, but an existing snapshot. Restore volume from snapshot
    elif snapshot and not volume and not has_pvc:
        snapshot_id = snapshot["SnapshotId"]
        snapshot_size = snapshot["VolumeSize"]

        log.info(
            f"PVC '{pvc_name}' does not exist for user '{username}'. Therefore a volume will be restored from '{snapshot_id}'."
        )

        # Guarantee that the volume never shrinks if the spawner's volume is smaller than the snapshot
        if snapshot_size > desired_vol_size:
            log.info(
                f"Spawner gives storage as {desired_vol_size}. Snapshot has volume {snapshot_size}. Expanding volume size to match snapshot."
            )
            desired_vol_size = snapshot_size

        vol = ec2.create_volume(
            AvailabilityZone=AZ_NAME,
            Encrypted=False,
            Size=desired_vol_size,
            SnapshotId=snapshot_id,  # It's important that the snapshot id exist to restore from
            VolumeType="gp3",
            DryRun=False,
            TagSpecifications=[
                {
                    "ResourceType": "volume",
                    "Tags": [
                        {
                            "Key": "Name",
                            "Value": f"user--{username}--{LAB_SHORT_NAME}",
                        },
                        {
                            "Key": f"kubernetes.io/cluster/{CLUSTER_NAME}",
                            "Value": "owned",
                        },
                        {
                            "Key": "kubernetes.io/created-for/pvc/namespace",
                            "Value": NAMESPACE,
                        },
                        {
                            "Key": "kubernetes.io/created-for/pvc/name",
                            "Value": pvc_name,
                        },
                        {"Key": "RestoredFromSnapshot", "Value": "true"},
                        {"Key": "is-jupyterhub-user", "Value": "true"},
                        # Volumes need to tagged for the CSI driver to properly manage them
                        {"Key": "ebs.csi.aws.com/cluster", "Value": "true"},
                        {"Key": "ebs.csi.aws.com/cluster-name", "Value": CLUSTER_NAME},
                    ],
                },
            ],
        )
        vol_id = vol["VolumeId"]
        log.info(f"Volume {vol_id} created.")

        # If do-not-delete tag was present in snapshot, add to volume tags
        if get_tag_value(snapshot, "do-not-delete"):
            ec2.create_tags(
                DryRun=False,
                Resources=[vol_id],
                Tags=[
                    {"Key": "do-not-delete", "Value": "True"},
                ],
            )

    # Case 5: No PVC, no existing volume, no existing snapshot. Create new volume.
    elif not volume and not snapshot and not has_pvc:
        log.info(
            f"PVC '{pvc_name}' does not exist for user '{username}'. Therefore a new volume will be created."
        )

        vol = ec2.create_volume(
            AvailabilityZone=AZ_NAME,
            Encrypted=False,
            Size=desired_vol_size,
            VolumeType="gp3",
            DryRun=False,
            TagSpecifications=[
                {
                    "ResourceType": "volume",
                    "Tags": [
                        {
                            "Key": "Name",
                            "Value": f"user--{username}--{LAB_SHORT_NAME}",
                        },
                        {
                            "Key": f"kubernetes.io/cluster/{CLUSTER_NAME}",
                            "Value": "owned",
                        },
                        {
                            "Key": "KubernetesCluster",
                            "Value": CLUSTER_NAME,
                        },
                        {
                            "Key": "kubernetes.io/created-for/pvc/namespace",
                            "Value": NAMESPACE,
                        },
                        {
                            "Key": "kubernetes.io/created-for/pvc/name",
                            "Value": pvc_name,
                        },
                        {"Key": COST_TAG_KEY, "Value": COST_TAG_VALUE},
                        {"Key": "RestoredFromSnapshot", "Value": "false"},
                        {"Key": "is-jupyterhub-user", "Value": "true"},
                        # Volumes need to tagged for the CSI driver to properly manage them
                        {"Key": "ebs.csi.aws.com/cluster", "Value": "true"},
                        {"Key": "ebs.csi.aws.com/cluster-name", "Value": CLUSTER_NAME},
                    ],
                },
            ],
        )
        vol_id = vol["VolumeId"]
        log.info(f"Volume {vol_id} created.")

    # After volume is created (either by previously existing, as new, or restored from snapshot), create PV and PVC

    # Explicitly annote the provisioner. The CSI plugin appears to not do this properly.
    annotations.update({"pv.kubernetes.io/provisioned-by": "ebs.csi.aws.com"})

    # The Storage Class and PVC schema used is defined in cluster_cdk_stack.py:jupyterhub_helm_values.singleuser.storage
    pvc_manifest = {
        "api_version": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "annotations": annotations,
            "cluster_name": CLUSTER_NAME,
            "labels": labels,
            "name": pvc_name,
            "namespace": NAMESPACE,
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": f"{desired_vol_size}Gi"}},
            "storageClassName": "gp3-jh-user",
            "volumeMode": "Filesystem",
            "volumeName": vol_id,
        },
    }

    pv_manifest = {
        "api_version": "v1",
        "kind": "PersistentVolume",
        "metadata": {
            "annotations": pvc_manifest["metadata"]["annotations"],
            "cluster_name": CLUSTER_NAME,
            "labels": {
                "topology.kubernetes.io/region": REGION_NAME,
                "topology.kubernetes.io/zone": AZ_NAME,
            },
            "name": vol_id,
            "namespace": NAMESPACE,
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "awsElasticBlockStore": {
                "fsType": "ext4",
                "volumeID": f"aws://{AZ_NAME}/{vol_id}",
            },
            "capacity": {
                "storage": pvc_manifest["spec"]["resources"]["requests"]["storage"]
            },
            "nodeAffinity": {
                "required": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {
                                    "key": "topology.kubernetes.io/zone",
                                    "operator": "In",
                                    "values": [AZ_NAME],
                                },
                                {
                                    "key": "topology.kubernetes.io/region",
                                    "operator": "In",
                                    "values": [REGION_NAME],
                                },
                            ]
                        }
                    ]
                }
            },
            "persistentVolumeReclaimPolicy": "Delete",
            "storageClassName": "gp3-jh-user",
            "volumeMode": "Filesystem",
            "claimRef": {"namespace": NAMESPACE, "name": pvc_name},
        },
    }

    # https://github.com/kubernetes-client/python/blob/master/kubernetes/docs/CoreV1Api.md#create_persistent_volume
    log.info("Creating persistent volume...")
    try:
        api.create_persistent_volume(body=pv_manifest)
    except ApiException as e:
        if e.status == 409:
            log.info(f"PV {vol_id} already exists, so did not create new pv.")
        else:
            raise

    # https://github.com/kubernetes-client/python/blob/master/kubernetes/docs/CoreV1Api.md#create_namespaced_persistent_volume_claim
    log.info("Creating persistent volume claim...")
    try:
        api.create_namespaced_persistent_volume_claim(
            body=pvc_manifest, namespace=NAMESPACE
        )
    except ApiException as e:
        if e.status == 409:
            log.info(f"PVC {pvc_name} already exists, so did not create new pvc.")
        else:
            raise

    # Make sure that the volume is tagged with required tags.
    ec2.create_tags(
        DryRun=False,
        Resources=[vol_id],
        Tags=[
            {"Key": "kubernetes.io/created-for/pv/name", "Value": vol_id},
            {"Key": "CSIVolumeName", "Value": vol_id},
            {"Key": "KubernetesCluster", "Value": CLUSTER_NAME},
            {"Key": "ebs.csi.aws.com/cluster", "Value": "true"},
        ],
    )


def server_starting_tag(pvc_name: str, **kwargs) -> None:
    session = boto3.Session(region_name=REGION_NAME)
    ec2 = session.client("ec2")

    log.info(f"Updating starting tags to '{pvc_name}' in cluster '{CLUSTER_NAME}'...")

    volume = get_volume_for_pvc(pvc_name=pvc_name, ec2=ec2)

    if volume:
        ec2.create_tags(
            DryRun=False,
            Resources=[volume["VolumeId"]],
            Tags=[
                {"Key": "server-start-time", "Value": _get_delta_time(days=0)},
            ],
        )
    else:
        log.info(f"No volumes found for '{pvc_name}'. Nothing to tag.")


async def my_pre_hook(spawner: c.Spawner) -> None:  # noqa: F821
    try:
        # Kubespawner overrides in the Profile List are enacted AFTER this hook script runs.
        # So we need to retrieve the user-selected profile and options
        user_options: dict = spawner.user_options
        log.info(f"User options selected: {user_options}")

        selected_profile_slug: str | None = user_options.get("profile")

        # Get the profile list (as defined in profiles.py)
        profile_list: list = spawner.profile_list
        if callable(profile_list):
            profile_list: list = await profile_list(spawner)  # type: ignore

        # Cycle through profiles until the right profile is found and then apply overrides
        for profile in profile_list:
            if profile["slug"] == selected_profile_slug:
                overrides = profile.get("kubespawner_override", {})
                spawner.storage_capacity = overrides.get("storage_capacity", "10Gi")

        # Get a object with pvc metadata that JupyterHub thinks you will need
        spawn_pvc = spawner.get_pvc_manifest()

        args = {
            "username": spawner.user.name,
            "pvc_name": spawner.pvc_name,
            "storage_capacity": spawner.storage_capacity,
            "annotations": spawn_pvc.metadata.annotations,
            "labels": spawn_pvc.metadata.labels,
        }

        get_user_volume(**args)
        server_starting_tag(**args)

    except Exception as e:
        log.error(e)
        log.error(traceback.format_exc())
        raise


# The variable "c" is a global variable representing the Config instance.
# This code will be appended to the end of the jupyterhub config.
# Linters like Flake8 often fail to recognize "magic" variables like "c".
# Therefore we apply "noqa: F821"
c.Spawner.pre_spawn_hook = my_pre_hook  # noqa: F821
