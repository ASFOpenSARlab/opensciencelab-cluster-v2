#!/usr/bin/env python3
import asyncio
import os
import logging

import boto3

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

REGION_NAME = os.environ["AWS_REGION"]
NAMESPACE = os.environ["K8s_NAMESPACE"]
PREFIX_NAMESPACE = os.environ["PREFIX_NAMESPACE"]


logging.basicConfig(
    format="%(asctime)s %(levelname)s (%(lineno)d) - %(message)s", level=logging.INFO
)
log = logging.getLogger(__name__)


async def ec2_username_tag(username: str, pod_name: str, **kwargs) -> None:
    """
    Executes AFTER the user's pod starts. Resolves the K8s Node to its
    actual AWS EC2 Instance ID and applies tags.

    WARNING: This function assumes that only one pod is running per user. If multiple pods are running for a single user,
    the EC2 instance will be tagged with the last pod that started.

    """

    # 1. Initialize Kubernetes API Client using the Hub's in-cluster credentials
    k8s_config.load_incluster_config()
    api = k8s_client.CoreV1Api()

    try:
        # 2. Query the K8s API to find out which node the user's pod was placed on
        node_name = None
        # Poll up to 30 seconds for the scheduler to assign a node
        for _ in range(30):
            live_pod = api.read_namespaced_pod(name=pod_name, namespace=NAMESPACE)
            if live_pod.spec.node_name:
                node_name = live_pod.spec.node_name
                break
            await asyncio.sleep(1)

        if not node_name:
            raise ValueError(f"Pod {pod_name} does not have a node assigned yet.")

        # 3. Read the Node specs to get the cloud provider data
        node = api.read_node(name=node_name)
        provider_id = node.spec.provider_id  # Format: aws:///<az>/i-0123456789abcdef0

        if not provider_id or "aws://" not in provider_id:
            raise ValueError(f"Node {node_name} does not have a valid AWS provider ID.")

        # 4. Parse out the exact EC2 Instance ID from the trailing path segment
        ec2_instance_id = provider_id.split("/")[-1]
        log.info(f"Mapping user {username} to EC2 instance: {ec2_instance_id}")

        # 5. Connect to AWS and Tag the EC2 Instance directly
        # Ensure your JupyterHub Hub Pod role has the 'ec2:CreateTags' IAM permission
        ec2_client = boto3.client("ec2", region_name=REGION_NAME)

        ec2_client.create_tags(
            Resources=[ec2_instance_id],
            Tags=[
                {"Key": f"{PREFIX_NAMESPACE}/username", "Value": username},
            ],
        )
        log.info(f"Successfully tagged EC2 instance {ec2_instance_id} for {username}")

    except Exception as e:  # noqa: BLE001
        log.error(f"Failed to tag EC2 instance for user {username}: {str(e)}")


async def my_after_pod_created_hook(spawner: c.Spawner, pod: dict):  # noqa: F821
    try:
        args = {
            "username": spawner.user.name,
            "pod_name": pod["metadata"]["name"],
        }

        await ec2_username_tag(**args)

    except Exception as e:  # noqa: BLE001
        log.error(f"An error occurred in the post-spawn hook: {str(e)}")


# The variable "c" is a global variable representing the Config instance.
# This code will be appended to the end of the jupyterhub config.
# Linters like Flake8 often fail to recognize "magic" variables like "c".
# Therefore we apply "noqa: F821"
c.Spawner.after_pod_created_hook = my_after_pod_created_hook  # noqa: F821
