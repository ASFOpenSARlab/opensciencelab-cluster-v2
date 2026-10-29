#!/usr/bin/env python3
import asyncio
import inspect
import logging
import os
import datetime

import boto3
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio import config as k8s_config

REGION_NAME = os.environ["AWS_REGION"]
NAMESPACE = os.environ["K8s_NAMESPACE"]
PREFIX_NAMESPACE = os.environ["PREFIX_NAMESPACE"]

logging.basicConfig(
    format="%(asctime)s %(levelname)s (%(lineno)d) - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


async def _call_kubernetes(method, **kwargs):
    """Call a sync or async Kubernetes method without blocking the event loop."""
    result = await asyncio.to_thread(method, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _close_api_client(api_client) -> None:
    """Close the client whether its close method is sync or async."""
    result = await asyncio.to_thread(api_client.close)
    if inspect.isawaitable(result):
        await result


def _tag_ec2_instance(instance_id: str, username: str) -> None:
    ec2_client = boto3.client("ec2", region_name=REGION_NAME)
    ec2_client.create_tags(
        Resources=[instance_id],
        Tags=[{"Key": f"{PREFIX_NAMESPACE}/username", "Value": username}],
    )


# WARNING: This function assumes that only one pod is running per user.
# If multiple user pods are running, the EC2 instance will be tagged with the last pod that started.
async def ec2_username_tag(username: str, pod_name: str, **kwargs) -> None:
    api_client = None
    start_time = datetime.datetime.now()

    try:
        # This configuration loader is synchronous.
        k8s_config.load_incluster_config()
        api_client = k8s_client.ApiClient()
        api = k8s_client.CoreV1Api(api_client)

        node_name = None
        timeout_seconds = 3600
        # get_running_loop() returns the event loop that is currently running in this thread.
        # If no event loop is running, it raises a RuntimeError. This is useful for checking if we are in an async context.
        deadline = asyncio.get_running_loop().time() + timeout_seconds

        # Get the pod's node name, retrying until we get it or timeout.
        while asyncio.get_running_loop().time() < deadline:
            live_pod = await _call_kubernetes(
                api.read_namespaced_pod,
                name=pod_name,
                namespace=NAMESPACE,
            )
            node_name = live_pod.spec.node_name
            if node_name:
                break
            await asyncio.sleep(3)

        if not node_name:
            raise TimeoutError(
                f"Pod {pod_name} was not assigned to a node within {timeout_seconds} seconds."
            )

        # Get the node's provider ID and extract the EC2 instance ID.
        node = await _call_kubernetes(api.read_node, name=node_name)
        provider_id = node.spec.provider_id

        if not provider_id or not provider_id.startswith("aws:///"):
            raise ValueError(f"Node {node_name} has an invalid AWS provider ID.")

        # The provider ID is in the format "aws:///REGION/INSTANCE_ID". We want to extract the INSTANCE_ID.
        instance_id = provider_id.rsplit("/", 1)[-1]
        if not instance_id.startswith("i-"):
            raise ValueError(
                f"Could not parse an EC2 instance ID from {provider_id!r}."
            )

        # Tag the EC2 instance with the username.
        log.info(f"Mapping user {username} to EC2 instance {instance_id}")
        await asyncio.to_thread(_tag_ec2_instance, instance_id, username)
        log.info(f"Successfully tagged EC2 instance {instance_id} for user {username}")

    except Exception:
        log.exception(f"Failed to tag EC2 instance for user {username}")

    finally:
        if api_client is not None:
            await _close_api_client(api_client)

        elapsed = (datetime.datetime.now() - start_time).total_seconds()
        log.info(f"EC2 tagging process for user {username} took {elapsed:.1f} seconds.")


async def my_after_pod_created_hook(spawner: c.Spawner, pod: dict) -> None:  # noqa: F821
    pod_name = pod.get("metadata", {}).get("name")
    if not pod_name:
        raise ValueError("Pod dictionary is missing 'metadata.name'")

    await ec2_username_tag(username=spawner.user.name, pod_name=pod_name)


# The variable "c" is a global variable representing the Config instance.
# This code will be appended to the end of the jupyterhub config.
# Linters like Flake8 often fail to recognize "magic" variables like "c".
# Therefore we apply "noqa: F821"
c.Spawner.after_pod_created_hook = my_after_pod_created_hook  # noqa: F821
