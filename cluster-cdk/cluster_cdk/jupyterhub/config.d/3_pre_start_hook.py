#!/usr/bin/env python3
import os
import datetime
import logging
import traceback

import boto3

CLUSTER_NAME = os.environ["CLUSTER_NAME"]
REGION_NAME = os.environ["AWS_REGION"]
AZ_NAME = os.environ["AZ_NAME"]
COST_TAG_KEY = os.environ["COST_TAG_KEY"]
COST_TAG_VALUE = os.environ["COST_TAG_VALUE"]


logging.basicConfig(
    format="%(asctime)s %(levelname)s (%(lineno)d) - %(message)s", level=logging.INFO
)
log = logging.getLogger(__name__)


def get_tag_value(resource, key):
    val = [s["Value"] for s in resource["Tags"] if s["Key"] == key]

    if not val:
        val = [""]

    return str(val[0])


def volume_from_snapshot(spawner):
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

    username = spawner.user.name
    pvc_name = spawner.pvc_name
    vol_size = spawner.storage_capacity
    spawn_pvc = spawner.get_pvc_manifest()

    namespace = "jupyter"

    log.info(
        f"Spawner gives storage as {vol_size}. If restoring from a snapshot, the size may be different."
    )

    alpha = " ".join(re.findall("[a-zA-Z]+", vol_size)).lower()
    number = int(" ".join(re.findall("[0-9]+", vol_size)))

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

    pvcs = api.list_namespaced_persistent_volume_claim(namespace=namespace, watch=False)

    has_pvc = False
    for items in pvcs.items:
        if items.metadata.name == pvc_name:
            log.warning(
                f"PVC '{pvc_name}' exists! Therefore a volume should have already been assigned to user '{username}'."
            )
            has_pvc = True

    if not has_pvc:
        log.warning(
            f"PVC '{pvc_name}' does not exist. Therefore a volume will have to be created for user '{username}'."
        )

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

        # Does the user have any volumes? If they do and don't have any PVC's, then something is wrong.
        # To avoid any data loss or corruptions by creating another volume for the user, throw an error to stop.
        if volume:
            raise Exception(
                f"Volume found for '{username}' without pvc '{pvc_name}'. This should not happen."
            )

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

        # Since there is no volume/pvc, restore from the snapshot
        if snapshot:
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
                                "Value": f"{username}-{CLUSTER_NAME}",
                            },
                            {
                                "Key": f"kubernetes.io/cluster/{CLUSTER_NAME}",
                                "Value": "owned",
                            },
                            {
                                "Key": "kubernetes.io/created-for/pvc/namespace",
                                "Value": namespace,
                            },
                            {
                                "Key": "kubernetes.io/created-for/pvc/name",
                                "Value": pvc_name,
                            },
                            {"Key": "RestoredFromSnapshot", "Value": "True"},
                        ],
                    },
                ],
            )
            vol_id = vol["VolumeId"]
            log.info(f"Volume {vol_id} created.")

            this_val = get_tag_value(snapshot, "jupyter-volume-stopping-time")
            if this_val:
                ec2.create_tags(
                    DryRun=False,
                    Resources=[vol_id],
                    Tags=[
                        {"Key": "jupyter-volume-stopping-time", "Value": this_val},
                    ],
                )

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

            annotations = spawn_pvc.metadata.annotations

            # Explicit annote the provisioner. The CSI plugin appears to not do this properly.
            # May not be needed
            # annotations.update({"pv.kubernetes.io/provisioned-by": "ebs.csi.aws.com"})

            labels = spawn_pvc.metadata.labels

            pvc_manifest = {
                "api_version": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {
                    "annotations": annotations,
                    "cluster_name": CLUSTER_NAME,
                    "labels": labels,
                    "name": pvc_name,
                    "namespace": namespace,
                },
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": f"{vol_size}Gi"}},
                    "storageClassName": "gp3",
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
                    "namespace": namespace,
                },
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "awsElasticBlockStore": {
                        "fsType": "ext4",
                        "volumeID": f"aws://{AZ_NAME}/{vol_id}",
                    },
                    "capacity": {
                        "storage": pvc_manifest["spec"]["resources"]["requests"][
                            "storage"
                        ]
                    },
                    "nodeAffinity": {
                        "required": {
                            "nodeSelectorTerms": [
                                {
                                    "matchExpressions": [
                                        {
                                            "key": "topology.kubernetes.io/zone",
                                            "operator": "In",
                                            "values": AZ_NAME,
                                        },
                                        {
                                            "key": "topology.kubernetes.io/region",
                                            "operator": "In",
                                            "values": AZ_NAME,
                                        },
                                    ]
                                }
                            ]
                        }
                    },
                    "persistentVolumeReclaimPolicy": "Delete",
                    "storageClassName": "gp3",
                    "volumeMode": "Filesystem",
                    "claimRef": {"namespace": namespace, "name": pvc_name},
                },
            }

            # https://github.com/kubernetes-client/python/blob/master/kubernetes/docs/CoreV1Api.md#create_persistent_volume
            log.info("Creating persistent volume...")
            try:
                api.create_persistent_volume(body=pv_manifest)
            except ApiException as e:
                if e.status == 409:
                    log.info(f"PV {vol_id} already exists, so did not create new pvc.")
                else:
                    raise

            # https://github.com/kubernetes-client/python/blob/master/kubernetes/docs/CoreV1Api.md#create_namespaced_persistent_volume_claim
            log.info("Creating persistent volume claim...")
            try:
                api.create_namespaced_persistent_volume_claim(
                    body=pvc_manifest, namespace=namespace
                )
            except ApiException as e:
                if e.status == 409:
                    log.info(
                        f"PVC {pvc_name} already exists, so did not create new pvc."
                    )
                else:
                    raise

        else:
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
                                "Value": f"{username}-{CLUSTER_NAME}",
                            },
                            {
                                "Key": f"kubernetes.io/cluster/{CLUSTER_NAME}",
                                "Value": "owned",
                            },
                            {
                                "Key": "kubernetes.io/created-for/pvc/namespace",
                                "Value": namespace,
                            },
                            {
                                "Key": "kubernetes.io/created-for/pvc/name",
                                "Value": pvc_name,
                            },
                            {"Key": "RestoredFromSnapshot", "Value": "False"},
                        ],
                    },
                ],
            )
            vol_id = vol["VolumeId"]
            log.info(f"Volume {vol_id} created.")

            ec2.create_tags(
                DryRun=False,
                Resources=[vol_id],
                Tags=[
                    {"Key": COST_TAG_KEY, "Value": COST_TAG_VALUE},
                ],
            )

            annotations = spawn_pvc.metadata.annotations

            # Explicit annote the provisioner. The CSI plugin appears to not do this properly.
            # May not be needed
            # annotations.update({"pv.kubernetes.io/provisioned-by": "ebs.csi.aws.com"})

            labels = spawn_pvc.metadata.labels

            pvc_manifest = {
                "api_version": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {
                    "annotations": annotations,
                    "cluster_name": CLUSTER_NAME,
                    "labels": labels,
                    "name": pvc_name,
                    "namespace": namespace,
                },
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": f"{vol_size}Gi"}},
                    "storageClassName": "gp3",
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
                    "namespace": namespace,
                },
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "awsElasticBlockStore": {
                        "fsType": "ext4",
                        "volumeID": f"aws://{AZ_NAME}/{vol_id}",
                    },
                    "capacity": {
                        "storage": pvc_manifest["spec"]["resources"]["requests"][
                            "storage"
                        ]
                    },
                    "nodeAffinity": {
                        "required": {
                            "nodeSelectorTerms": [
                                {
                                    "matchExpressions": [
                                        {
                                            "key": "topology.kubernetes.io/zone",
                                            "operator": "In",
                                            "values": AZ_NAME,
                                        },
                                        {
                                            "key": "topology.kubernetes.io/region",
                                            "operator": "In",
                                            "values": AZ_NAME,
                                        },
                                    ]
                                }
                            ]
                        }
                    },
                    "persistentVolumeReclaimPolicy": "Delete",
                    "storageClassName": "gp3",
                    "volumeMode": "Filesystem",
                    "claimRef": {"namespace": namespace, "name": pvc_name},
                },
            }

            # https://github.com/kubernetes-client/python/blob/master/kubernetes/docs/CoreV1Api.md#create_persistent_volume
            log.info("Creating persistent volume...")
            try:
                api.create_persistent_volume(body=pv_manifest)
            except ApiException as e:
                if e.status == 409:
                    log.info(f"PV {vol_id} already exists, so did not create new pvc.")
                else:
                    raise

            # https://github.com/kubernetes-client/python/blob/master/kubernetes/docs/CoreV1Api.md#create_namespaced_persistent_volume_claim
            log.info("Creating persistent volume claim...")
            try:
                api.create_namespaced_persistent_volume_claim(
                    body=pvc_manifest, namespace=namespace
                )
            except ApiException as e:
                if e.status == 409:
                    log.info(
                        f"PVC {pvc_name} already exists, so did not create new pvc."
                    )
                else:
                    raise


def server_starting_tag(spawner):
    pvc_name = spawner.pvc_name

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


def my_pre_hook(spawner):
    try:
        volume_from_snapshot(spawner)
        server_starting_tag(spawner)

    except Exception as e:
        log.error(e)
        log.error(traceback.format_exc())
        raise


c.Spawner.pre_spawn_hook = my_pre_hook  # noqa: F821
