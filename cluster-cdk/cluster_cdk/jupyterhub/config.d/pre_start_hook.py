#!/usr/bin/env python3
import os
import datetime
import logging
import traceback

import boto3

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


def get_tag_value(resource, key):
    val = [s["Value"] for s in resource["Tags"] if s["Key"] == key]

    if not val:
        val = [""]

    return str(val[0])


def get_volume(
    username: str, pvc_name: str, storage_capacity: str, annotations: dict, labels: dict
) -> None:
    """
    # Before mounting the home directory, check to see if a PVC exists.
    # If it does, then assume there is a EBS volume associated.
    # If it doesn't, check for any EBS snapshots.
    # If a snapshot exists, create a volume from the snapshot.
    # Else, JupyterHub will create the volume.
    """

    import re

    import boto3

    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config
    from kubernetes.client.rest import ApiException

    k8s_config.load_incluster_config()
    api = k8s_client.CoreV1Api()

    log.info(
        f"Spawner gives storage as {storage_capacity}. If restoring from a snapshot, the size may be different."
    )

    # Convert the desired volume size into a standarized value
    alpha = " ".join(re.findall("[a-zA-Z]+", storage_capacity)).lower()
    number = int(" ".join(re.findall("[0-9]+", storage_capacity)))

    possible_units = {
        "ei": 2**60,
        "pi": 2**50,
        "ti": 2**40,
        "gi": 2**30,
        "mi": 2**20,
        "ki": 2**10,
        "e": 10**18,
        "p": 10**15,
        "t": 10**12,
        "g": 10**9,
        "m": 10**6,
        "k": 10**3,
        "": 1,
    }

    vol_size = number * possible_units[alpha]

    # Volume needs to be in GiB (an int without the label)
    vol_size = int(vol_size * 2**-30)
    if vol_size < 0:
        vol_size = 1

    session = boto3.Session(region_name=REGION_NAME)
    ec2 = session.client("ec2")

    pvcs = api.list_namespaced_persistent_volume_claim(namespace=NAMESPACE, watch=False)

    has_pvc = False
    for items in pvcs.items:
        if items.metadata.name == pvc_name:
            log.warning(
                f"PVC '{pvc_name}' exists! Therefore a volume should have already been assigned to user '{username}'."
            )
            has_pvc = True

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
        volumes = sorted(volumes, key=lambda s: s["CreateTime"], reverse=True)
        log.warning(
            f"\nWARNING ***** More than one volume found for pvc name: {pvc_name}. Claiming the latest one: \n{volumes[0]}."
        )
    elif len(volumes) == 0:
        log.info(f"No volumes found that matched pvc name '{pvc_name}'")
        volumes = [None]

    volume = volumes[0]

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

    snapshot = snap[0]

    # If PVC does exist, assume volume does as well.

    if not has_pvc:
        vol_id = None

        # If a volume doesn't exist but a snapshot does, restore from snapshot and create PVC
        if not volume and snapshot:
            log.warning(
                f"PVC '{pvc_name}' does not exist. Therefore a volume will have to be created for user '{username}'."
            )

            # Guarantee that the volume never shrinks if the spawner's volume is smaller than the snapshot
            if snapshot["VolumeSize"] > vol_size:
                vol_size = snapshot["VolumeSize"]

            log.info("Creating volume from snapshot...")
            vol = ec2.create_volume(
                AvailabilityZone=AZ_NAME,
                Encrypted=False,
                Size=vol_size,
                SnapshotId=snapshot["SnapshotId"],
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

            # If the billing tag is present in the snapshot, add to volume tags
            # If the tag doesn't exist in the snapshot, the default is `COST_TAG_VALUE`
            this_val = get_tag_value(snapshot, COST_TAG_KEY)
            if not this_val:
                this_val = COST_TAG_VALUE
            ec2.create_tags(
                DryRun=False,
                Resources=[vol_id],
                Tags=[
                    {"Key": COST_TAG_KEY, "Value": this_val},
                ],
            )

        elif not volume and not snapshot:
            log.info("Creating new volume...")
            vol = ec2.create_volume(
                AvailabilityZone=AZ_NAME,
                Encrypted=False,
                Size=vol_size,
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
                            {"Key": COST_TAG_KEY, "Value": COST_TAG_VALUE},
                            {"Key": "RestoredFromSnapshot", "Value": "false"},
                        ],
                    },
                ],
            )
            vol_id = vol["VolumeId"]
            log.info(f"Volume {vol_id} created.")

        # If a volume exists create PVC for volume.
        elif volume:
            log.warning(
                f"Volume found for '{username}' without pvc '{pvc_name}'. This should not happen."
            )

            vol_id = volume["VolumeId"]
            vol_size = volume["Size"]

        # After volume is created (either by previously existing, as new, or restored from snapshot), create PV and PVC

        # Explicit annote the provisioner. The CSI plugin appears to not do this properly.
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
                "resources": {"requests": {"storage": f"{vol_size}Gi"}},
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

    vol = ec2.describe_volumes(
        Filters=[
            {"Name": "tag:kubernetes.io/created-for/pvc/name", "Values": [pvc_name]},
            {
                "Name": f"tag:kubernetes.io/cluster/{CLUSTER_NAME}",
                "Values": ["owned"],
            },
        ]
    )

    vol = vol["Volumes"]

    if len(vol) > 1:
        raise Exception(f"\n ***** More than one volume for pvc: {pvc_name}")

    if len(vol) != 1:
        vol = []
    else:
        vol = vol[0]

    if vol:
        ec2.create_tags(
            DryRun=False,
            Resources=[vol["VolumeId"]],
            Tags=[
                {
                    "Key": "server-start-time",
                    "Value": str(
                        datetime.datetime.now(datetime.timezone.utc).replace(
                            second=0, microsecond=0
                        )
                    ),
                },
            ],
        )


def my_pre_hook(spawner: c.Spawner) -> None:  # noqa: F821
    try:
        # Get a object with pvc metadata that JupyterHub thinks you will need
        spawn_pvc = spawner.get_pvc_manifest()

        args = {
            "username": spawner.user.name,
            "pvc_name": spawner.pvc_name,
            "storage_capacity": spawner.storage_capacity,
            "annotations": spawn_pvc.metadata.annotations,
            "labels": spawn_pvc.metadata.labels,
        }

        get_volume(**args)
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
