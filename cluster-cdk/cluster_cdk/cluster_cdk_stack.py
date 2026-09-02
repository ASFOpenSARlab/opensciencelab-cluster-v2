import os
import tomllib  # type: ignore
import pathlib
from string import Template
import json
import re

import requests

# Import the monitoring library constructs
import cdk_monitoring_constructs as monitoring  # type: ignore

from aws_cdk import (  # type: ignore
    CfnTag,
    CfnOutput,
    custom_resources as cr,
    Tags,
    RemovalPolicy,
    Duration,
    Stack,
    SecretValue,
    aws_s3 as s3,
    aws_cloudwatch as cloudwatch,
    aws_eks_v2 as eks,
    aws_ec2 as ec2,
    aws_dlm as dlm,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_secretsmanager as secretsmanager,
    aws_events as events,
    aws_events_targets as targets,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subs,
    lambda_layer_kubectl_v34,
    lambda_layer_awscli,
)

from constructs import Construct  # type: ignore


class ClusterCdkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        #####################################################################
        #
        #    Setup Environment
        #
        #####################################################################

        self.LAB_SHORT_NAME = os.environ["LAB_SHORT_NAME"]
        cluster_name = os.environ["LAB_SHORT_NAME"]

        self.HOME_DIR = pathlib.Path(__file__).absolute().parent

        # CDK provides the AWS Account number via self.account # "233535791844"
        # CDK provides the AWS Region va self.region

        self.JUPYTER_HUB_IMAGE_PATH = os.environ["JUPYTER_HUB_IMAGE_PATH"]
        self.JUPYTER_HUB_IMAGE_TAG = os.environ["JUPYTER_HUB_IMAGE_TAG"]

        self.UI_IAM_ROLE = os.environ["UI_IAM_ROLE"]

        # Default cron schedule to top of every hour
        self.VOLUME_CRON_SCHEDULE = os.environ["VOLUME_CRON_SCHEDULE"]
        self.SNAPSHOT_WARNING_DAYS = os.environ["SNAPSHOT_WARNING_DAYS"]
        # Defaults SNAPSHOT_GRACEPERIOD_DAYS if provided value is empty string
        self.SNAPSHOT_GRACEPERIOD_DAYS = (
            os.environ["SNAPSHOT_GRACEPERIOD_DAYS"] or "1.0"
        )

        self.ADMIN_USERS = [
            username.strip() for username in os.environ["ADMIN_USERS"].split(",")
        ]

        self.PORTAL_DOMAINS = os.environ["PORTAL_DOMAINS"]

        self.DAYS_TILL_VOLUME_DELETION = os.environ["DAYS_TILL_VOLUME_DELETION"]

        self.DAYS_TILL_SNAPSHOT_DELETION = os.environ["DAYS_TILL_SNAPSHOT_DELETION"]

        # Make sure everything happens in a particular AZ.
        # This is normally 'a' but can be 'b' or 'c' if more than one cluster is deployed in an account and resources will be limited.
        self.AZ_LETTER = os.getenv("AZ_LETTER", None)
        if not self.AZ_LETTER:
            self.AZ_LETTER = "a"

        self.K8s_NAMESPACE = "jupyter"

        # Get nodes and profiles
        profiles = tomllib.loads(os.environ["PROFILE_DEFINITIONS"])
        nodes = tomllib.loads(os.environ["NODE_DEFINITIONS"])

        # Put config data into a format better for code interactions
        # { "name": "hello", "attr": "value", ... }
        self.lab_profiles = [{"name": name} | body for name, body in profiles.items()]
        self.lab_nodes = [{"name": name} | body for name, body in nodes.items()]

        # All resources in this specific stack will get this tag
        Tags.of(self).add("osl-billing", self.LAB_SHORT_NAME)  # type: ignore

        self.kubectl_layer = lambda_layer_kubectl_v34.KubectlV34Layer(self, "kubectl")

        ########
        #
        #  Parameters related to cryptnono
        #
        ########
        self.CRYPTNONO_ALERT_EMAIL = os.getenv("CRYPTNONO_ALERT_EMAIL", None)
        self.EXECWHACKER_CRON_IMAGE_PATH = os.getenv(
            "EXECWHACKER_CRON_IMAGE_PATH", None
        )
        self.EXECWHACKER_CRON_IMAGE_TAG = os.getenv("EXECWHACKER_CRON_IMAGE_TAG", None)

        # Be somewhat aggressive in only enabling cryptnono if explicitly "true" and EXECWAHCKER path and tag are defined
        self.IS_CRYPTNONO_ENABLED = (
            os.getenv("IS_CRYPTNONO_ENABLED", "false").strip().lower() == "true"
            and self.EXECWHACKER_CRON_IMAGE_PATH
            and self.EXECWHACKER_CRON_IMAGE_TAG
            and True
        )

        # See what vars are defined within this context
        print("vars within CDK...")
        for k, v in vars(self).items():
            print(k, v)
        print("....")

        #####################################################################
        #
        #    Setup Networking
        #
        #####################################################################

        # Two subnets for EKS
        public_subnet = ec2.SubnetConfiguration(
            name="PublicSubnet",
            subnet_type=ec2.SubnetType.PUBLIC,
            cidr_mask=24,
        )
        private_subnet = ec2.SubnetConfiguration(
            name="PrivateSubnetWithEgress",
            subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
            cidr_mask=24,
        )

        # Create a custom VPC restricted to the single AZ
        self.vpc = ec2.Vpc(
            self,
            "EksVPC",
            availability_zones=[f"{self.region}{self.AZ_LETTER}", f"{self.region}d"],
            ip_addresses=ec2.IpAddresses.cidr("10.0.0.0/16"),
            # Configure subnet types for EKS (e.g., Public and Private)
            subnet_configuration=[public_subnet, private_subnet],
        )

        #####################################################################
        #
        #    Setup Cluster
        #
        #####################################################################

        ## https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_eks_v2/README.html#provisioning-clusters
        self.cluster = eks.Cluster(
            self,
            "EksCluster",
            vpc=self.vpc,
            cluster_name=cluster_name,
            version=eks.KubernetesVersion.V1_34,
            kubectl_provider_options=eks.KubectlProviderOptions(
                kubectl_layer=self.kubectl_layer,
            ),
            default_capacity_type=eks.DefaultCapacityType.NODEGROUP,
            default_capacity=0,
        )

        # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_eks/README.html#add-ons
        eks.Addon(
            self,
            "CniAddon",
            addon_name="vpc-cni",
            addon_version="v1.20.4-eksbuild.2",
            cluster=self.cluster,
            # configuration_values={},
        )
        eks.Addon(  # Check if needed
            self,
            "CoreDnsAddon",
            addon_name="coredns",
            addon_version="v1.12.3-eksbuild.1",
            cluster=self.cluster,
            # configuration_values={},
        )

        ## Look up latest default version of amazon-cloudwatch-observability:
        # aws eks describe-addon-versions \
        #   --addon-name amazon-cloudwatch-observability \
        #   --kubernetes-version 1.34 \
        #   --query "addons[0].addonVersions[?compatibilities[0].defaultVersion]"
        self.cw_observe_addon = eks.Addon(
            self,
            "CloudwatchObserv",
            addon_name="amazon-cloudwatch-observability",
            addon_version="v4.10.2-eksbuild.1",
            cluster=self.cluster,
        )

        self.cw_observe_addon.node.add_dependency(self.cluster)

        eks.Addon(  # Check if needed
            self,
            "KubeProxyAddon",
            addon_name="kube-proxy",
            addon_version="v1.34.0-eksbuild.2",
            cluster=self.cluster,
            # configuration_values={},
        )

        #####################################################################
        #
        #    Setup Secret Manager values
        #
        #####################################################################

        sso_token_secret_name = f"sso-token/eks-cluster-{self.LAB_SHORT_NAME}"

        self.sso_token = secretsmanager.Secret(
            self,
            "SSOTokenSecret",
            secret_name=sso_token_secret_name,
            description="SSO Token to communicate with the Portal",
            secret_string_value=SecretValue.unsafe_plain_text(
                "ReplaceMeOrYouWillAlwaysFail"
            ),
            # removal_policy=None  # Don't set removal policy so that the following custom resource can delete the secret
        )

        ####
        #  Make sure that the SSO Token secret is destroyed immediately instead of waiting days
        #
        cr.AwsCustomResource(
            self,
            "DeleteSSOSecretCR",
            on_delete=cr.AwsSdkCall(
                service="SecretsManager",
                action="deleteSecret",
                parameters={
                    "SecretId": self.sso_token.secret_arn,
                    "ForceDeleteWithoutRecovery": True,  # Optional: Deletes immediately
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    self.sso_token.secret_arn
                ),
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=["secretsmanager:DeleteSecret"],
                        resources=[self.sso_token.secret_arn],
                        effect=iam.Effect.ALLOW,
                    )
                ]
            ),
        )

        #####################################################################
        #
        #    Setup Nodegroups
        #
        #    These are paired 1-to-1 with auto scaling groups but are better managed by k8s.
        #
        #####################################################################

        # https://github.com/aws/aws-cdk/issues/37012eks.Cluster
        for node in self.lab_nodes:
            node_type = node.get("node_type", "user")
            node_name_escaped = re.sub(r"[^A-Za-z0-9]", "00", node["name"].strip())

            # Node labels to apply depending on node type
            node_labels = node.get("labels", {})
            if node_type == "core":
                node_labels["hub.jupyter.org/node-purpose"] = "core"
                node_labels["opensciencelab.local/node-type"] = "core"

            elif node_type == "user":
                node_labels["hub.jupyter.org/node-purpose"] = "user"
                node_labels["opensciencelab.local/node-type"] = (
                    f"user-{node_name_escaped}"
                )
                node_labels["opensciencelab.local/cryptnono-enabled"] = str(
                    self.IS_CRYPTNONO_ENABLED
                ).lower()

            # Root volume of EC2 defaults to 20GiB. If defined as something else, it must be within EBS's storage range.
            root_volume_size = int(node.get("root_volume_size", "20"))
            if root_volume_size < 1:
                raise Exception(
                    f"root_volume_size has value of {root_volume_size} and is less than 1 GiB"
                )
            elif root_volume_size > 16345:
                raise Exception(
                    f"root_volume_size has value of {root_volume_size} and is greater than 16345 GiB"
                )

            # Define the Launch Template with the desired EC2 instance tags
            # These tags will be applied to the EC2 instances when they are launched by the Auto Scaling Group
            launch_template = ec2.CfnLaunchTemplate(
                self,
                f"{node['name']}-LaunchTemplate-{self.LAB_SHORT_NAME}",
                launch_template_data=ec2.CfnLaunchTemplate.LaunchTemplateDataProperty(
                    # Configure Block Device Mappings (Storage)
                    # https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/block-device-mapping-concepts.html
                    # Note that the volume size here must be equal or bigger than the node's AMI's root volume snapshot
                    # Otherwise you will get an error like "Volume of size 10GB is smaller than snapshot 'snap-013c0e96ad509b9e2', expect size >= 20GB"
                    block_device_mappings=[
                        ec2.CfnLaunchTemplate.BlockDeviceMappingProperty(
                            device_name="/dev/xvda",
                            ebs=ec2.CfnLaunchTemplate.EbsProperty(
                                volume_size=root_volume_size,
                                volume_type="gp3",
                                encrypted=False,
                                delete_on_termination=True,
                            ),
                        )
                    ],
                    metadata_options=ec2.CfnLaunchTemplate.MetadataOptionsProperty(
                        http_put_response_hop_limit=2,  # Set hop limit here
                        http_tokens="required",  # Recommended for IMDSv2
                    ),
                    tag_specifications=[
                        ec2.CfnLaunchTemplate.TagSpecificationProperty(
                            resource_type="instance",
                            tags=[
                                CfnTag(key="osl-billing", value=self.LAB_SHORT_NAME),
                                CfnTag(
                                    key="Name",
                                    value=f"{node['name']}--{self.LAB_SHORT_NAME}",
                                ),
                            ],
                        ),
                        ec2.CfnLaunchTemplate.TagSpecificationProperty(
                            resource_type="volume",
                            tags=[
                                CfnTag(key="osl-billing", value=self.LAB_SHORT_NAME),
                                CfnTag(
                                    key="Name",
                                    value=f"{node['name']}-root--{self.LAB_SHORT_NAME}",
                                ),
                            ],
                        ),
                    ],
                ),
            )

            # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_eks/NodegroupOptions.html
            node_group = self.cluster.add_nodegroup_capacity(
                f"{node['name']}{self.LAB_SHORT_NAME}",
                ami_type=eks.NodegroupAmiType.AL2023_X86_64_STANDARD,
                capacity_type=eks.CapacityType.ON_DEMAND,
                max_size=node.get("group_max_size"),
                min_size=node.get("group_min_size"),
                # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ec2/InstanceClass.html
                instance_types=[
                    ec2.InstanceType(instance) for instance in node["instance"]
                ],
                launch_template_spec=eks.LaunchTemplateSpec(
                    id=launch_template.ref,
                    version=launch_template.attr_latest_version_number,
                ),
                # Force the compute in the public subnet, in a single AZ
                # This also automagically adds the "k8s.io/cluster-autoscaler/CLUSTER_NAME: owned" tag to the ASG and thus EC2s
                subnets=ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PUBLIC,
                    availability_zones=[
                        f"{self.region}{self.AZ_LETTER}"
                    ],  # Force compute into one AZ
                ),
                labels=node_labels,
            )

            # SMCE required observability policies for all nodes
            for managed_policy in [
                "AmazonSSMManagedInstanceCore",
                "CloudWatchAgentAdminPolicy",
                "CloudWatchAgentServerPolicy",
            ]:
                node_group.role.add_managed_policy(
                    iam.ManagedPolicy.from_aws_managed_policy_name(managed_policy)
                )

            # The ec2 ROLE must have the tag "eks-cluster-name=CLUSTER_NAME" for the CSI IAM condtions
            Tags.of(node_group.role).add("eks-cluster-name", cluster_name)

            if node_type == "core":
                policies = [
                    {
                        "Sid": "HubDescribe",
                        "Effect": "Allow",
                        "Action": [
                            "ec2:DescribeSnapshots",
                            "ec2:DescribeVolumes",
                            "ec2:DescribeImages",
                            "ec2:DescribeInstanceTypes",
                            "ec2:DescribeLaunchTemplateVersions",
                            "eks:DescribeNodegroup",
                        ],
                        "Resource": "*",
                    },
                    {
                        "Sid": "HubTagsOnVolumeCreation",
                        "Effect": "Allow",
                        "Action": ["ec2:CreateTags"],
                        "Resource": [
                            f"arn:aws:ec2:{self.region}:{self.account}:volume/*",
                        ],
                        "Condition": {
                            "StringEquals": {
                                # If the cluster tag is passed in the request payload, it MUST match this cluster
                                "aws:RequestTag/ebs.csi.aws.com/cluster-name": cluster_name,
                                "ec2:CreateAction": "CreateVolume",
                            },
                        },
                    },
                    {
                        "Sid": "HubUpdateTags",
                        "Effect": "Allow",
                        "Action": ["ec2:CreateTags"],
                        "Resource": [
                            f"arn:aws:ec2:{self.region}:{self.account}:volume/*",
                        ],
                        "Condition": {
                            "StringEquals": {
                                # If updating an existing volume, it MUST already belong to this cluster
                                "ec2:ResourceTag/ebs.csi.aws.com/cluster-name": cluster_name
                            },
                        },
                    },
                    {
                        "Sid": "HubVolumeCreate",
                        "Effect": "Allow",
                        "Action": ["ec2:CreateVolume"],
                        "Resource": [
                            f"arn:aws:ec2:{self.region}:{self.account}:volume/*",
                        ],
                        "Condition": {
                            "StringEquals": {
                                "aws:RequestTag/ebs.csi.aws.com/cluster-name": cluster_name
                            }
                        },
                    },
                    {
                        "Sid": "HubVolumeCreateFromSnapshot",
                        "Effect": "Allow",
                        "Action": ["ec2:CreateVolume"],
                        "Resource": [
                            f"arn:aws:ec2:{self.region}::snapshot/*",
                        ],
                        "Condition": {
                            "StringEquals": {
                                "ec2:ResourceTag/ebs.csi.aws.com/cluster-name": cluster_name
                            }
                        },
                    },
                    {
                        "Sid": "HubSecretsManagerRead",
                        "Effect": "Allow",
                        "Action": ["secretsmanager:GetSecretValue"],
                        "Resource": self.sso_token.secret_arn,
                    },
                    {
                        "Sid": "AutoscalerDescribe",
                        "Effect": "Allow",
                        "Action": [
                            "autoscaling:DescribeAutoScalingGroups",
                            "autoscaling:DescribeAutoScalingInstances",
                            "autoscaling:DescribeLaunchConfigurations",
                            "autoscaling:DescribeScalingActivities",
                            "autoscaling:DescribeTags",
                        ],
                        "Resource": "*",
                    },
                    {
                        "Sid": "AutoscalerAutoscaling",
                        "Effect": "Allow",
                        "Action": [
                            "autoscaling:SetDesiredCapacity",
                            "autoscaling:TerminateInstanceInAutoScalingGroup",
                        ],
                        "Resource": "*",
                        "Condition": {
                            "StringEquals": {
                                # EC2s need to be tagged "eks:cluster-name=CLUSTER_NAME". EKS managed nodegroup automatically does this.
                                "aws:ResourceTag/eks:cluster-name": cluster_name,
                            }
                        },
                    },
                ]
                for policy in policies:
                    node_group.role.add_to_policy(iam.PolicyStatement.from_json(policy))

                # Needed so we can make a dependency later
                self.core_nodegroup = node_group

        #####################################################################
        #
        #    Setup EBS CSI Storage for volume creation
        #
        #    Once storage classes are implemented on the cluster, they cannot be updated in place.
        #    For example, if a tag specification within parameters is changed and the cluster is redeployed, a deployment error will occur.
        #    Existing storage classes will need to be deleted manually before redeploying the cluster.
        #    Deleting the storage class will not break existing volumes but will make it difficult to create new volumes. So promptness is essential.
        #
        #    To delete a storage class manually within cloudshell ...
        #       View all storage classes: `kubectl get sc`
        #       Deleted desired storage class: `kubectl delete sc STORAGE_CLASS_NAME`
        #
        #####################################################################

        # Storage class for user volumes
        self.cluster.add_manifest(
            "CsiStorageClass",
            {
                "apiVersion": "storage.k8s.io/v1",
                "kind": "StorageClass",
                "metadata": {
                    "name": "gp3-jh-user",
                    "annotations": {
                        "storageclass.kubernetes.io/is-default-class": "true",
                    },
                },
                "provisioner": "ebs.csi.aws.com",
                "parameters": {
                    "type": "gp3",
                    "fsType": "ext4",
                },
                "allowVolumeExpansion": True,
                "volumeBindingMode": "Immediate",
                "reclaimPolicy": "Delete",
            },
        )

        # Storage class for JupyterHub DB. Ensures that it is nicely named in the EC2 console
        self.cluster.add_manifest(
            "JhDbStorageClass",
            {
                "apiVersion": "storage.k8s.io/v1",
                "kind": "StorageClass",
                "metadata": {
                    "name": "gp3-jh-db",
                },
                "provisioner": "ebs.csi.aws.com",
                "parameters": {
                    "type": "gp3",
                    "fsType": "ext4",
                    "tagSpecification_1": f"osl-billing={self.LAB_SHORT_NAME}",
                    "tagSpecification_2": f"Name=hub-db-dir--{self.LAB_SHORT_NAME}",
                    "tagSpecification_3": "is-jupyterhub-db=true",
                    # The CSI driver is expecting volumes to be tagged a certain way
                    "tagSpecification_4": "ebs.csi.aws.com/cluster=true",
                    "tagSpecification_5": f"ebs.csi.aws.com/cluster-name={cluster_name}",
                },
                "allowVolumeExpansion": False,
                "volumeBindingMode": "Immediate",
                "reclaimPolicy": "Delete",
            },
        )

        # CSI storage
        csi_service_account = self.cluster.add_service_account(
            "EbsCsiServiceAccount",
            name="ebs-csi-controller-sa",
            namespace="kube-system",
            overwrite_service_account=True,
        )

        # EC2s need to be tagged "eks:cluster-name=CLUSTER_NAME". EKS manged nodegroup automatically does this.
        # Volumes need to tagged "ebs.csi.aws.com/cluster=true" and "ebs.csi.aws.com/cluster-name=CLUSTER_NAME". These tags are injected from the CSI driver or the custom storage classes.
        # The AmazonEBSCSIDriverEKSClusterScopedPolicy strictly enforces that the value of the resource tag ebs.csi.aws.com/cluster-name on your EBS volumes must match an eks-cluster-name tag on the IAM principal (the role).
        Tags.of(csi_service_account.role).add("eks-cluster-name", cluster_name)

        # Attach the official pre-built managed policy to the principal role
        csi_service_account.role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "AmazonEBSCSIDriverEKSClusterScopedPolicy"
            )
        )

        self.csi_driver_version = "2.56.1"

        # https://artifacthub.io/packages/helm/aws-ebs-csi-driver/aws-ebs-csi-driver
        self.ebs_csi_driver_helm_chart = self.cluster.add_helm_chart(
            "AwsEbsCsiDriver",
            repository="https://kubernetes-sigs.github.io/aws-ebs-csi-driver",
            atomic=True,
            chart="aws-ebs-csi-driver",
            release=f"ebs-csi-driver-{self.LAB_SHORT_NAME}",  # type: ignore
            namespace="kube-system",
            version=self.csi_driver_version,
            wait=True,
            timeout=Duration.minutes(8),
            values={
                "controller": {
                    "extraCreateMetadata": True,
                    "k8sTagClusterId": cluster_name,
                    "extraVolumeTags": {
                        "osl-billing": self.LAB_SHORT_NAME,
                    },
                    "serviceAccount": {
                        "create": False,
                        "name": csi_service_account.service_account_name,
                    },
                    "nodeSelector": {"opensciencelab.local/node-type": "core"},
                },
            },
        )

        if self.UI_IAM_ROLE:
            # Access Entry for EKS UI
            user_access_ui_entry = eks.AccessEntry(
                self,
                "UserAccessUI",
                access_policies=[
                    eks.AccessPolicy.from_access_policy_name(
                        "AmazonEKSClusterAdminPolicy",
                        access_scope_type=eks.AccessScopeType.CLUSTER,
                    ),
                ],
                cluster=self.cluster,
                principal=f"arn:aws:iam::{self.account}:role/{self.UI_IAM_ROLE}",
                access_entry_type=eks.AccessEntryType.STANDARD,
                removal_policy=RemovalPolicy.DESTROY,
            )

            self.ebs_csi_driver_helm_chart.node.add_dependency(user_access_ui_entry)

        #####################################################################
        #
        #    Setup JupyterHub
        #
        #####################################################################

        self.jupyterhub_helm_version = "4.3.2"

        # Modify the k8s permissions so the volumes can be modified in place
        # Patching existing clusterroles is difficult. So we are fully replacing the original from jupyterhub.
        # This particular action adds the verb "patch" to PVC
        # Original can be found in the Kubernetes git repo https://github.com/kubernetes/kubernetes/blob/release-1.36/plugin/pkg/auth/authorizer/rbac/bootstrappolicy/policy.go#L501
        # and https://github.com/kubernetes/kubernetes/blob/8f8aa9aae157b88db6ba02836c57596496d3f684/plugin/pkg/auth/authorizer/rbac/bootstrappolicy/testdata/cluster-roles.yaml#L1320
        eks.KubernetesManifest(
            self,
            "PVProvisionerClusterRole",
            cluster=self.cluster,
            overwrite=True,
            manifest=[
                {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "ClusterRole",
                    "metadata": {
                        "annotations": {
                            "rbac.authorization.kubernetes.io/autoupdate": "true"
                        },
                        "labels": {"kubernetes.io/bootstrapping": "rbac-defaults"},
                        "name": "system:persistent-volume-provisioner",
                    },
                    "rules": [
                        {
                            "apiGroups": [""],
                            "resources": ["persistentvolumes"],
                            "verbs": ["create", "delete", "get", "list", "watch"],
                        },
                        {
                            "apiGroups": [""],
                            "resources": ["persistentvolumeclaims"],
                            "verbs": ["get", "list", "update", "watch", "patch"],
                        },
                        {
                            "apiGroups": ["storage.k8s.io"],
                            "resources": ["storageclasses"],
                            "verbs": ["get", "list", "watch"],
                        },
                        {
                            "apiGroups": [""],
                            "resources": ["events"],
                            "verbs": ["watch"],
                        },
                        {
                            "apiGroups": ["", "events.k8s.io"],
                            "resources": ["events"],
                            "verbs": ["create", "patch", "update"],
                        },
                    ],
                }
            ],
        )

        # Make sure the hook volume scripts (via the hub service account) have the right volume provisioner permissions
        self.cluster.add_manifest(
            "PVClusterRoleBinding",
            {
                "kind": "ClusterRoleBinding",
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "metadata": {"name": "cluster-pv"},
                "subjects": [
                    {
                        "kind": "ServiceAccount",
                        "name": "hub",
                        "namespace": self.K8s_NAMESPACE,
                    }
                ],
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole",
                    "name": "system:persistent-volume-provisioner",
                },
            },
        )

        # For deeper configuration explanation, refer to ARCHITECTURE.md
        jupyterhub_helm_values = {
            "cull": {
                "enabled": True,
                "timeout": 1800,  # Cull user servers after 1800 seconds (30 minutes) of inactivity
                "every": 300,  # Check for idle servers every 300 seconds (5 minutes)
                "maxAge": 259200,  # Maximum age in seconds (3 days) before culling regardless of activity
                "users": False,  # Cull users in addition to their servers
                "adminUsers": False,  # Set to true to also cull admin users
            },
            "prePuller": {
                "continuous": {"enabled": False},
                "hook": {"enabled": False},
            },
            "scheduling": {
                "userPlaceholder": {"enabled": False},
                "userScheduler": {
                    "enabled": True,
                    "labels": {"sidecar.istio.io/inject": "false"},
                },
                "corePods": {"nodeAffinity": {"matchNodePurpose": "require"}},
                "userPods": {"nodeAffinity": {"matchNodePurpose": "require"}},
            },
            # https://z2jh.jupyter.org/en/stable/resources/reference.html#singleuser
            # Usually, general and default pod, volume, and kubespawner settings should be set here.
            # If profile specific settings are needed, set within `config.d/profiles.py`
            "singleuser": {
                # This might not be needed anymore. Actually, not sure why this was added but something broke on upgrading to AL2023 and this fixed it at the time.
                # "cloudMetadata": {
                #     # For some reason IP Tables isn't working properly anymore on AL2023
                #     # So disable blocking cloud metadata which uses IP Tables
                #     # For safety, the metadata IP is blocked on the Istio level.
                #     "blockWithIptables": False
                # },
                # https://z2jh.jupyter.org/en/stable/resources/reference.html#singleuser-storage
                "storage": {
                    "dynamic": {
                        "storageClass": "gp3-jh-user",
                        # This {username} is a template used by jupyterhub and is not an f-string
                        # For possible template values: https://jupyterhub-kubespawner.readthedocs.io/en/latest/templates.html#templated-fields
                        "pvcNameTemplate": "claim-{username}",
                    },
                },
                "extraPodConfig": {
                    # By default, Kubernetes recursively changes the ownership of every single file in a mounted volume to match the pod's fsGroup. On very large volumes, this chown operation can take 15–45+ minutes, causing the pod to hang in a ContainerCreating or initializing state.
                    # When set to OnRootMismatch, Kubernetes will only change the file ownership and permissions if the root directory of the volume does not match the expected fsGroup. If the permissions on the root already match, it completely skips the slow recursive check.
                    "securityContext": {
                        "fsGroup": 100,
                        "fsGroupChangePolicy": "OnRootMismatch",
                    },
                },
                "extraFiles": (
                    {}
                    | self._set_extra_file(
                        "user_server_includes/hooks/default.sh",
                        "shell",
                        "/etc/user_server_includes/hooks/default.sh",
                    )
                    | self._set_extra_file(
                        "user_server_includes/overrides/default.json",
                        "file",
                        "/etc/user_server_includes/overrides/default.json",
                    )
                ),
            },
            "hub": {
                "image": {
                    "name": self.JUPYTER_HUB_IMAGE_PATH,
                    "tag": self.JUPYTER_HUB_IMAGE_TAG,
                    "pullPolicy": "Always",
                },
                "db": {
                    "pvc": {
                        "storageClassName": "gp3-jh-db",
                    }
                },
                "baseUrl": f"/lab/{self.LAB_SHORT_NAME}",
                "config": {
                    "JupyterHub": {
                        "default_url": f"/lab/{self.LAB_SHORT_NAME}/hub/home",
                        "tornado_settings": {
                            "cookie_options": {"expires_days": 1.0},
                        },
                    },
                    "Authenticator": {
                        "admin_users": self.ADMIN_USERS,
                        "auth_refresh_age": 60,
                        "allow_all": True,
                        "enable_auth_state": True,
                    },
                    "KubeSpawner": {
                        # https://jupyterhub-kubespawner.readthedocs.io/en/latest/spawner.html#kubespawner.KubeSpawner.http_timeout
                        "http_timeout": 30,
                        # https://jupyterhub-kubespawner.readthedocs.io/en/latest/spawner.html#kubespawner.KubeSpawner.start_timeout
                        "start_timeout": 600,
                        # https://jupyterhub-kubespawner.readthedocs.io/en/latest/spawner.html#kubespawner.KubeSpawner.pod_name_template
                        # This is not an f-string but a templated string used by jupyterhub.
                        "pod_name_template": "jupyter-{username}",
                        # https://jupyterhub-kubespawner.readthedocs.io/en/latest/spawner.html#kubespawner.KubeSpawner.delete_pvc
                        "delete_pvc": False,
                        "slug_scheme": "escape",
                    },
                    # Other kubespawner values are set per lab profile at ./jupyterhub/config.d/profiles.py
                },
                # All extraEnv need to be strings
                "extraEnv": {
                    "AWS_REGION": self.region,
                    "SSO_TOKEN_ARN": self.sso_token.secret_arn,
                    "SSO_TOKEN_PATH": "/tmp/sso_token",
                    "OPENSARLAB_SSO_TOKEN_PATH": "/tmp/sso_token",
                    "LAB_SHORT_NAME": self.LAB_SHORT_NAME,
                    "JUPYTERHUB_LAB_PREFIX": f"/lab/{self.LAB_SHORT_NAME}",
                    "PORTAL_DOMAINS": self.PORTAL_DOMAINS,
                    "LAB_PROFILES": json.dumps(self.lab_profiles),
                    "DAYS_TILL_VOLUME_DELETION": self.DAYS_TILL_VOLUME_DELETION,
                    "DAYS_TILL_SNAPSHOT_DELETION": self.DAYS_TILL_SNAPSHOT_DELETION,
                    "CLUSTER_NAME": cluster_name,
                    "AZ_NAME": f"{self.region}{self.AZ_LETTER}",
                    "COST_TAG_KEY": "osl-billing",
                    "COST_TAG_VALUE": self.LAB_SHORT_NAME,
                    "K8s_NAMESPACE": self.K8s_NAMESPACE,
                },
                "extraFiles": (
                    {}
                    | self._set_extra_file(
                        "jupyterhub/hub_home.html.j2",
                        "html",
                        "/usr/local/share/jupyterhub/templates/custom/page.html",
                    )
                    | self._set_extra_file(
                        "jupyterhub/portal_auth.py",
                        "python",
                        "/usr/local/lib/python3.12/site-packages/jupyterhub/portal_auth.py",
                    )
                    | self._set_extra_file(
                        "jupyterhub/config.d/auth.py",
                        "python",
                        "/usr/local/etc/jupyterhub/jupyterhub_config.d/auth.py",
                    )
                    | self._set_extra_file(
                        "jupyterhub/config.d/extras.py",
                        "python",
                        "/usr/local/etc/jupyterhub/jupyterhub_config.d/extras.py",
                    )
                    | self._set_extra_file(
                        "jupyterhub/config.d/profiles.py",
                        "python",
                        "/usr/local/etc/jupyterhub/jupyterhub_config.d/profiles.py",
                    )
                    | self._set_extra_file(
                        "jupyterhub/config.d/pre_start_hook.py",
                        "python",
                        "/usr/local/etc/jupyterhub/jupyterhub_config.d/pre_start_hook.py",
                    )
                    | self._set_extra_file(
                        "jupyterhub/config.d/post_stop_hook.py",
                        "python",
                        "/usr/local/etc/jupyterhub/jupyterhub_config.d/post_stop_hook.py",
                    )
                ),
            },
            "proxy": {
                "https": {"enabled": False},
                "service": {
                    "type": "ClusterIP",
                },
            },
        }

        # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_eks/README.html#helm-charts
        # https://artifacthub.io/packages/helm/jupyterhub/jupyterhub?modal=values-schema
        # https://z2jh.jupyter.org/en/latest/resources/reference.html
        self.jupyerhub_helm_chart = self.cluster.add_helm_chart(
            "JupyterhubHelmChart",
            repository="https://jupyterhub.github.io/helm-chart/",
            atomic=False,
            chart="jupyterhub",
            release=f"jupyterhub-{self.LAB_SHORT_NAME}",  # type: ignore
            version=self.jupyterhub_helm_version,
            namespace=self.K8s_NAMESPACE,
            wait=True,
            timeout=Duration.minutes(10),
            values=jupyterhub_helm_values,
        )

        self.jupyerhub_helm_chart.node.add_dependency(self.ebs_csi_driver_helm_chart)

        #####################################################################
        #
        #    Setup Load Balancer
        #
        #####################################################################

        # The default CDK AWS Controller is woefully out of date.
        # Use the helm chart
        self.load_balancer_controller_version = "3.2.1"

        alb_sa = self.cluster.add_service_account(
            "aws-load-balancer-controller-sa", namespace="kube-system"
        )

        alb_controller_url = f"https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/v{self.load_balancer_controller_version}/docs/install/iam_policy.json"
        policy_json = requests.get(url=alb_controller_url).json()

        for statement in policy_json["Statement"]:
            alb_sa.add_to_principal_policy(iam.PolicyStatement.from_json(statement))

        load_balancer_helm_chart = self.cluster.add_helm_chart(
            "ALBController",
            chart="aws-load-balancer-controller",
            repository="https://aws.github.io/eks-charts",
            namespace="kube-system",
            wait=True,  # Until the pods are ready
            timeout=Duration.minutes(10),
            version=self.load_balancer_controller_version,
            values={
                "clusterName": cluster_name,
                "serviceAccount": {
                    "create": False,
                    "name": alb_sa.service_account_name,
                },
                "region": self.region,
                "vpcId": self.cluster.vpc.vpc_id,
                "nodeSelector": {"opensciencelab.local/node-type": "core"},
                "enableWaf": False,
                "enableWafv2": False,
                "defaultTags": {"AlbControllerManaged": True},
            },
        )

        # Create namespace jupyterhub proxy for load balancer service
        # Annotations on the service will create a NLB selecting for the jupyterhub proxy pod
        #
        # WARNING: Before deleting the stack, you MUST open AWS CloudShell and manually run `kubectl -n jupyter delete svc proxy-public-loadbalancer`.
        # For reasons unknown, having CDK auto-delete the k8s service on destroy does not safely delete all the networking resources
        load_balancer_manifest = self.cluster.add_manifest(
            "JupyterHubNLBService",
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": "proxy-public-loadbalancer",
                    "namespace": self.K8s_NAMESPACE,
                    "labels": {
                        "app": "jupyterhub",
                        "opensciencelab.local/node-type": "core",
                    },
                    "annotations": {
                        "service.beta.kubernetes.io/aws-load-balancer-name": f"eks-cluster-{self.LAB_SHORT_NAME}",
                        "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type": "ip",
                        "service.beta.kubernetes.io/aws-load-balancer-scheme": "internet-facing",
                        "service.beta.kubernetes.io/aws-load-balancer-type": "external",
                        "service.beta.kubernetes.io/aws-load-balancer-healthcheck-path": "/hub/health",
                        "service.beta.kubernetes.io/aws-load-balancer-healthcheck-healthy-threshold": "3",
                    },
                },
                "spec": {
                    "ports": [
                        {"protocol": "TCP", "port": 80, "targetPort": 8000},
                    ],
                    "selector": {
                        "app": "jupyterhub",
                        "component": "proxy",
                    },
                    "type": "LoadBalancer",
                },
            },
        )

        # The nlb manifest is dependant on jupyterhub for the jupyter namespace.
        # And is dependent on the load balancer controller to delete the NLB when the coreesponding k8s service is deleted.
        # Since such relationships are not explicit in the cdk code above, we need to declare the relationships here.
        # "If the controller pod is deleted before the Ingress object (e.g., during a cdk destroy),
        #  the Ingress will remain in a "Terminating" state, and the ALB will be orphaned."
        load_balancer_manifest.node.add_dependency(self.core_nodegroup)
        load_balancer_manifest.node.add_dependency(self.jupyerhub_helm_chart)
        load_balancer_manifest.node.add_dependency(alb_sa)
        load_balancer_manifest.node.add_dependency(load_balancer_helm_chart)

        # Since the NLB is created via annotations, we need to get the url after jupyterhub installation.
        self.nlb_url = self.cluster.get_service_load_balancer_address(
            "proxy-public-loadbalancer",
            namespace=self.K8s_NAMESPACE,
            timeout=Duration.minutes(15),
        )

        #####################################################################
        #
        #    Alerts / Observability
        #
        #####################################################################

        self.alert_sns_topic = sns.Topic(
            self,
            f"{self.LAB_SHORT_NAME} Cluster Alerts",
            display_name=f"{self.LAB_SHORT_NAME} Cluster Alerts",
            topic_name=f"{self.LAB_SHORT_NAME}-cluster-alerts-sns",
        )

        #####################################################################
        #
        #    Volume/Snapshot Management
        #
        #####################################################################

        self.volume_management_lambda = lambda_.Function(
            self,
            description=f"{self.LAB_SHORT_NAME} Volume Management Lambda",
            id=f"{self.LAB_SHORT_NAME}_volume_management",
            runtime=lambda_.Runtime.PYTHON_3_13,
            memory_size=1769,
            timeout=Duration.minutes(15),
            handler="volume_management.lambda_handler",
            code=lambda_.Code.from_asset(
                path="cluster_cdk/lambdas/",
            ),
            environment={
                "CLUSTER_NAME": cluster_name,
                "LAB_SHORT_NAME": self.LAB_SHORT_NAME,
                "SNAPSHOT_WARNING_DAYS": self.SNAPSHOT_WARNING_DAYS,
                "SNAPSHOT_GRACEPERIOD_DAYS": self.SNAPSHOT_GRACEPERIOD_DAYS,
                "PORTAL_DOMAINS": self.PORTAL_DOMAINS,
                "SSO_SECRET_ARN": self.sso_token.secret_arn,
                "ALERT_SNS_TOPIC_ARN": self.alert_sns_topic.topic_arn,
            },
        )

        self.requirements_layer = lambda_.LayerVersion(
            self,
            "RequirementsLayer",
            # /tmp/.build/lambda/ is make in the Makefile @ bundle-deps
            code=lambda_.Code.from_asset("/tmp/.build/lambda/"),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_13],
        )

        # Add awscli and requirements lambda layers
        self.volume_management_lambda.add_layers(
            lambda_layer_awscli.AwsCliLayer(self, "AwsCliLayer")
        )
        self.volume_management_lambda.add_layers(self.requirements_layer)

        # Grant lambda role access to EKS
        eks.AccessEntry(
            self,
            "lambda_eks_access",
            access_policies=[
                eks.AccessPolicy.from_access_policy_name(
                    "AmazonEKSClusterAdminPolicy",
                    access_scope_type=eks.AccessScopeType.CLUSTER,
                ),
            ],
            cluster=self.cluster,
            principal=self.volume_management_lambda.role.role_arn,
            access_entry_type=eks.AccessEntryType.STANDARD,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.volume_management_lambda.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["eks:*"],
                resources=[self.cluster.cluster_arn],
            )
        )

        # grant lambda the access it needs
        self.alert_sns_topic.grant_publish(self.volume_management_lambda)
        self.sso_token.grant_read(self.volume_management_lambda)
        self.volume_management_lambda.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "ec2:DescribeVolumes",
                    "ec2:DescribeSnapshots",
                    "ec2:CreateSnapshot",
                    "ec2:DeleteSnapshot",
                    "ec2:CreateTags",
                ],
                resources=["*"],
            )
        )

        self.cron_schedule_rule = events.Rule(
            self,
            id=f"{self.LAB_SHORT_NAME}_volume_management_rule",
            schedule=events.Schedule.expression(f"cron({self.VOLUME_CRON_SCHEDULE})"),
            description="Triggers Volume Management Lambda",
        )

        self.cron_schedule_rule.add_target(
            targets.LambdaFunction(self.volume_management_lambda)
        )

        # DLM configuration to create daily volume snapshots
        self.dlm_role = iam.Role(
            self,
            id=f"{self.LAB_SHORT_NAME}_dlm_service_role",
            assumed_by=iam.ServicePrincipal("dlm.amazonaws.com"),
        )

        # Attach the AWS-managed policy for DLM execution
        self.dlm_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSDataLifecycleManagerServiceRole"
            )
        )

        # Create DLM Policy - https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_dlm.html
        self.ebs_lifecycle_policy = dlm.CfnLifecyclePolicy(
            self,
            id=f"{self.LAB_SHORT_NAME}_daily_snapshot",
            description="Daily backup policy for EBS volumes",
            execution_role_arn=self.dlm_role.role_arn,
            state="ENABLED",
            policy_details=dlm.CfnLifecyclePolicy.PolicyDetailsProperty(
                resource_types=["VOLUME"],
                # Target volumes from this cluster only
                target_tags=[
                    CfnTag(key="KubernetesCluster", value=cluster_name),
                ],
                schedules=[
                    dlm.CfnLifecyclePolicy.ScheduleProperty(
                        name="DailySnapshots",
                        tags_to_add=[
                            CfnTag(
                                key="CreatedBy",
                                value=f"{self.LAB_SHORT_NAME}_daily_snapshot",
                            ),
                        ],
                        create_rule=dlm.CfnLifecyclePolicy.CreateRuleProperty(
                            interval=24,  # every 24 hours
                            interval_unit="HOURS",
                            times=["12:00"],  # Start window time (UTC HH:MM format)
                        ),
                        retain_rule=dlm.CfnLifecyclePolicy.RetainRuleProperty(count=1),
                        copy_tags=True,
                    )
                ],
            ),
        )

        #####################################################################
        #
        #    Setup Cryptnono
        #
        #    Cryptnono is a libarary that checks JupyterLab terminal inputs for cryptomining commands. https://github.com/cryptnono/cryptnono.
        #
        #    It contains many parts of which
        #
        #    Execwhacker - Monitor jupyterlab terminals for prohibited processes and kill them.
        #
        #    A list of prohibitted processes are stored in s3. This list is occasionally pulled into a configmap used by the execwhacker sidecar.
        #
        #
        #    To manually run the cronjob from within cloudshell:
        #        kubectl -n cryptnono create job --from=cronjob/update-execwhacker-config-cronjob execwacker-manual-refesh
        #
        #    To test, open user JupyterLab server terminal and run command `sh -c 'sleep 1 && echo thisisabannedstring'`. This will return `Killed`.
        #
        #    Get logs in pod:
        #        kubectl -n cryptnono logs -l app.kubernetes.io/instance=cryptnono -c execwhacker
        #
        #    Due to the nature of CDK, we can enable/disable cryptnono via changesets using simple 'if' statements. This allows for clean configuration code.
        #
        #####################################################################

        if self.IS_CRYPTNONO_ENABLED:
            # Add Cryptnono namespace to k8s. Include k8s permissions.
            cryptnono_ns_manifest = self.cluster.add_manifest(
                "CustomCryptnonoNamespace",
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": "cryptnono"},
                },
            )

            # Modify the k8s permissions so the pods in the cryptnono namespace can do things
            eks.KubernetesManifest(
                self,
                "CustomCryptnonoClusterRole",
                cluster=self.cluster,
                overwrite=True,
                manifest=[
                    {
                        "apiVersion": "rbac.authorization.k8s.io/v1",
                        "kind": "ClusterRole",
                        "metadata": {
                            "annotations": {
                                "rbac.authorization.kubernetes.io/autoupdate": "true"
                            },
                            "labels": {"kubernetes.io/bootstrapping": "rbac-defaults"},
                            "name": "custom-cryptnono",
                        },
                        "rules": [
                            {
                                # Permissions for execwhacker configmap update
                                "apiGroups": [""],
                                "resources": [
                                    "nodes",
                                    "pods",
                                    "events",
                                    "configmaps",
                                ],
                                "verbs": [
                                    "get",
                                    "watch",
                                    "list",
                                    "create",
                                    "delete",
                                    "patch",
                                    "update",
                                ],
                            },
                        ],
                    }
                ],
            )

            self.cluster.add_manifest(
                "CustomCryptnonoRoleBinding",
                {
                    "kind": "ClusterRoleBinding",
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "metadata": {"name": "custom-cryptnono"},
                    "subjects": [
                        {
                            "kind": "ServiceAccount",
                            "name": "default",
                            "namespace": "cryptnono",
                            "apiGroup": "",  # apiGroup is ""(core/v1) for service_account
                        }
                    ],
                    "roleRef": {
                        "apiGroup": "rbac.authorization.k8s.io",
                        "kind": "ClusterRole",
                        "name": "custom-cryptnono",
                    },
                },
            )

            # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_s3/Bucket.html
            # Bucket that contains configmap files used by cryptnono
            # Bucket prefix name cannot be more than 38 characters long
            bucket_name_prefix = f"crypt-conf-{self.LAB_SHORT_NAME}"[0:36].lower()

            execwhacker_bucket = s3.Bucket(
                self,
                "ExecwhackerConfigsBucket",
                bucket_name_prefix=bucket_name_prefix,
                bucket_namespace=s3.BucketNamespace.ACCOUNT_REGIONAL,
                versioned=True,
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
                removal_policy=RemovalPolicy.DESTROY,
                auto_delete_objects=True,
            )

            self.execwhacker_bucket_name = execwhacker_bucket.bucket_name

            # Add required policies to Core nodegroup
            execwhacker_s3_policies = [
                {
                    "Sid": "ExecwhackerS3ListAllBuckets",
                    "Effect": "Allow",
                    "Action": [
                        "s3:ListAllMyBuckets",
                    ],
                    "Resource": "*",
                },
                {
                    "Sid": "ExecwhackerS3ListBucket",
                    "Effect": "Allow",
                    "Action": [
                        "s3:ListBucket",
                    ],
                    "Resource": execwhacker_bucket.bucket_arn,
                },
                {
                    "Sid": "ExecwhackerS3ReadOnly",
                    "Effect": "Allow",
                    "Action": [
                        "s3:GetObject",
                        "s3:GetObjectAcl",
                        "s3:GetObjectVersion",
                    ],
                    "Resource": f"{execwhacker_bucket.bucket_arn}/*",
                },
            ]
            for policy in execwhacker_s3_policies:
                self.core_nodegroup.role.add_to_policy(
                    iam.PolicyStatement.from_json(policy)
                )

            # Execwhacker Cron Variables
            execwhacker_cron_schedule = "*/10 * * * *"  # Runs every 10 minutes
            execwhacker_args = f'python3 /app/update_execwhacker_config.py --aws-region={self.region} --config-bucket-name="{execwhacker_bucket.bucket_name}"'

            # k8s Cronjob that pulls from s3 and updates configmap in cluster
            execwhacker_manifest = self.cluster.add_manifest(
                "UpdateExecwhackerConfigCronJobManifest",
                {
                    "apiVersion": "batch/v1",
                    "kind": "CronJob",
                    "metadata": {
                        "name": "update-execwhacker-config-cronjob",
                        "namespace": "cryptnono",
                    },
                    "spec": {
                        "schedule": execwhacker_cron_schedule,
                        "concurrencyPolicy": "Forbid",
                        "successfulJobsHistoryLimit": 2,
                        "failedJobsHistoryLimit": 1,
                        "jobTemplate": {
                            "spec": {
                                "template": {
                                    "spec": {
                                        "restartPolicy": "OnFailure",
                                        "nodeSelector": {
                                            "opensciencelab.local/node-type": "core"
                                        },
                                        "containers": [
                                            {
                                                "name": "update-execwhacker-worker",
                                                "image": f"{self.EXECWHACKER_CRON_IMAGE_PATH}:{self.EXECWHACKER_CRON_IMAGE_TAG}",
                                                "imagePullPolicy": "Always",
                                                "command": ["sh", "-c"],
                                                "args": [execwhacker_args],
                                            }
                                        ],
                                    }
                                }
                            }
                        },
                    },
                },
            )

            execwhacker_manifest.node.add_dependency(cryptnono_ns_manifest)

            # Install crytnono helm chart with values
            cryptnono_helm_values = {
                "nodeSelector": {"opensciencelab.local/cryptnono-enabled": "true"},
                "detectors": {
                    "execwhacker": {
                        "configs": {
                            "noop": {"bannedCommandStrings": []},
                            "data": {
                                "bannedCommandStrings": [],
                                "allowedCommandPatterns": [],
                            },
                        }
                    }
                },
            }

            self.cryptnono_helm_chart = self.cluster.add_helm_chart(
                "CryptnonoHelmChart",
                repository="https://cryptnono.github.io/cryptnono/",
                atomic=False,
                chart="cryptnono",
                release="cryptnono",  # type: ignore
                version="v0.3.1",
                namespace="cryptnono",
                wait=True,
                timeout=Duration.minutes(2),
                values=cryptnono_helm_values,
            )

            self.cryptnono_helm_chart.node.add_dependency(execwhacker_manifest)

        #####################################################################
        #
        #    Cryptnono CloudWatch Notifications
        #
        #    When cloudwatch receive logs matching `log_processed.action = "killed"` and `kubernetes.container_name = "execwhacker"` an alarm will be triggered.
        #    This alarm will show up on the cloudwatch dashboard and will also trigger a SNS topic. Attached to this topic is an email.
        #
        #####################################################################

        jupyter_application_log_group_name = (
            f"/aws/containerinsights/{cluster_name}/application"
        )

        # Cryptnono notifications are looking within the jupyter applications log group
        # Since there is no guarantee that the log group will exist when cryptnono is deployed, we can create the log group if it doesn't exist.
        # If it already exists, it will gracefully apply the removal and retention (never delete) policies without crashing.
        jupyter_application_log_group_retention = logs.LogRetention(
            self,
            "CryptnonoJupyterHubAppSafeLogGroup",
            log_group_name=jupyter_application_log_group_name,
            retention=logs.RetentionDays.INFINITE,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Check the existing jupyter log group (created as needed from the previous action)
        cryptnono_log_group = logs.LogGroup.from_log_group_name(
            self,
            "CryptnonoJupyterHubAppLogs",
            jupyter_application_log_group_name,
        )

        # Create the messaging topic
        # Note that any changes in the display name must be accompied by a change in the topic anme. For some reason, changing only one is disliked.
        cryptnono_email_topic = sns.Topic(
            self,
            "CryptnonoKillEventAlertTopic",
            display_name=f"{cluster_name} - Cryptnono Kill Event Alert",
            topic_name=f"{cluster_name}-cryptnono-alert-sns",
        )

        # Subscribe your inbox (AWS will send a confirmation email you must click)
        cryptnono_email_topic.add_subscription(
            sns_subs.EmailSubscription(self.CRYPTNONO_ALERT_EMAIL)
        )

        # Scan for a specific text fragment within the log group. If found, count as one event
        cryptnono_metric_filter = logs.MetricFilter(
            self,
            "CryptnonoTextMatcherFilter",
            log_group=cryptnono_log_group,
            metric_namespace=f"{cluster_name}-Cryptnono",
            metric_name="Cryptnono Kill Event Count",
            filter_pattern=logs.FilterPattern.literal(
                '{ ( $.log_processed.action = "killed" && $.kubernetes.container_name = "execwhacker" ) }'
            ),
            metric_value="1",  # Increment by 1 for every single match
            unit=cloudwatch.Unit.COUNT,
        )

        # Make sure the log group is created before the metrics filter
        cryptnono_metric_filter.node.add_dependency(
            jupyter_application_log_group_retention
        )

        log_metric_group = monitoring.CustomMetricGroup(
            title="CryptnonoKillEventCount",
            metrics=[
                monitoring.CustomMetricWithAlarm(
                    metric=cryptnono_metric_filter.metric(
                        statistic="Sum",
                        period=Duration.minutes(1),
                        label=f"Count Cryptnono Kill Event for {cluster_name}",
                    ),
                    alarm_friendly_name="KillEventCountAlarm",
                    add_alarm={
                        "Critical": monitoring.CustomThreshold(
                            threshold=1,
                            evaluation_periods=1,
                            datapoints_to_alarm=1,
                            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                            alarm_name_override=f"{cluster_name} - Cryptnono Kill Event Match",
                            alarm_description_override="Discovered cryptnono matching patterns inside application logs",
                        )
                    },
                )
            ],
        )

        # The monitoring facade is a framework for CloudWatch dashboards, alerts, and other actions
        # https://github.com/cdklabs/cdk-monitoring-constructs
        # Bind your SNS topic as the global fallback action strategy to the monitoring facade
        cryptnono_facade = monitoring.MonitoringFacade(
            self,
            f"{cluster_name}-CryptnonoKillEventMonitoring",
            alarm_factory_defaults=monitoring.AlarmFactoryDefaults(
                actions_enabled=True,
                alarm_name_prefix=f"{cluster_name}-CryptnonoLogs",
                action=monitoring.SnsAlarmActionStrategy(
                    on_alarm_topic=cryptnono_email_topic
                ),
            ),
        )

        # Pass the metric filter data directly to the dashboard framework
        cryptnono_facade.monitor_custom(
            metric_groups=[log_metric_group],
            alarm_friendly_name=f"{cluster_name} - KillEventCountGroup",
            human_readable_name=f"{cluster_name} - Cryptnono Kill Event",
        )

        #####################################################################
        #
        #   A cloudwatch insights SQL query that will match a cryptnono event with an username.
        #
        #   There is an assumption that any user nodes have one jupyter user pod and one cryptnono sidecar pod.
        #
        #####################################################################

        cw_log_group = "/aws/containerinsights/eml/application"

        crytnono_query = f"""
            SELECT DISTINCT
                sidecar.`@timestamp`,
                sidecar.`log_processed.cmdline` as command,
                sidecar.`log_processed.matched` as matched,
                sidecar.`kubernetes.pod_name` as pod_name_sidecar,
                user_.`kubernetes.pod_name` as pod_name_user,
                sidecar.`kubernetes.host` as host,
                sidecar.`@message`
            FROM `{cw_log_group}` as sidecar
            INNER JOIN `{cw_log_group}` as user_
                ON sidecar.`kubernetes.host` = user_.`kubernetes.host`
            WHERE sidecar.`@message` like '%killed%'
                AND user_.`kubernetes.pod_name` like 'jupyter-%'
                AND CAST(sidecar.`@timestamp` as int) BETWEEN CAST(user_.`@timestamp` as int)-10 AND CAST(user_.`@timestamp` as int)+10
            ORDER BY sidecar.`@timestamp` DESC
            LIMIT 1000;
        """

        # Create a CloudWatch Dashboard
        sql_dashboard = cloudwatch.Dashboard(
            self,
            "CryptnonoSQLDashboard",
            dashboard_name=f"{cluster_name}-CryptnonoKillEventSQLQuery",
        )

        self.sql_dashboard_url = f"https://{self.region}.console.aws.amazon.com/cloudwatch/home?region={self.region}#dashboards:name={sql_dashboard.dashboard_name}"

        # Define your SQL-based Log Insights Widget
        sql_widget = cloudwatch.LogQueryWidget(
            title="Cryptnono Kill Event SQL Query",
            log_group_names=[cw_log_group],
            view=cloudwatch.LogQueryVisualizationType.TABLE,
            # Set the query language mode to SQL
            query_language=cloudwatch.LogQueryLanguage.SQL,
            # Pass your raw SQL query string
            query_string=crytnono_query,
            width=24,
            height=6,
        )

        # Attach the widget to the dashboard layout
        sql_dashboard.add_widgets(sql_widget)

        #####################################################################
        #
        #    Setup EC2 Autoscaler
        #
        #    Autoscale down unused EC2s
        #
        #####################################################################

        self.autoscaler_helm_version = "9.58.0"

        # Note that other args are added via ASG tags
        autoscaler_helm_chart_values = {
            "autoDiscovery": {"clusterName": cluster_name},
            "awsRegion": self.region,
            "nodeSelector": {"hub.jupyter.org/node-purpose": "core"},
            "cloudProvider": "aws",
            # List of extraArgs: https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/FAQ.md#what-are-the-parameters-to-ca
            "extraArgs": {
                "ignore-daemonsets-utilization": "true",
                "scale-down-unneeded-time": "2m0s",
                "scale-down-utilization-threshold": "0.5",
                "scale-down-delay-after-add": "1m0s",
            },
        }

        # https://artifacthub.io/packages/helm/cluster-autoscaler/cluster-autoscaler
        self.cluster.add_helm_chart(
            "ClusterAutoscaler",
            chart="cluster-autoscaler",
            repository="https://kubernetes.github.io/autoscaler",
            namespace="autoscaler",
            wait=True,  # Until the pods are ready
            atomic=False,
            timeout=Duration.minutes(2),
            version=self.autoscaler_helm_version,
            values=autoscaler_helm_chart_values,
        )

        #####################################################################
        #
        #    Setup CDK Outputs
        #
        #####################################################################

        CfnOutput(
            self,
            "NLB URL",
            value=f"http://{self.nlb_url}",
            description="The url of the Network Load Balancer",
        )

        CfnOutput(
            self,
            "NLB Controller Helm Version",
            value=self.load_balancer_controller_version,
            description="The version of the AWS Load Balancer Controller Helm Chart",
        )

        CfnOutput(
            self,
            "JupyterHub Helm Version",
            value=self.jupyterhub_helm_version,
            description="The version of the JupyterHub Helm Chart version",
        )

        if self.IS_CRYPTNONO_ENABLED:
            CfnOutput(
                self,
                "Cryptnono Config Bucket Name",
                value=self.execwhacker_bucket_name,
                description="Configs for the Cryptnono Execwhacker",
            )

            CfnOutput(
                self,
                "Cryptnono SQL Dashboard",
                value=self.sql_dashboard_url,
                description="Insights SQL query on Cryptnono events",
            )

    def _set_extra_file(
        self,
        file_path: str,
        file_type: str,
        mount_path: str,
        file_mode: str = "0644",
        extra_args: dict = {},
    ) -> dict[str, dict[str, str | bytes | dict | int]]:
        """
        Helper function to get files and setup for helm chart injection

        https://z2jh.jupyter.org/en/stable/resources/reference.html#singleuser-extrafiles
        https://z2jh.jupyter.org/en/stable/resources/reference.html#hub-extrafiles


        extra_args: A dicionary of string values to be subsituted into stringData file.

            ```
                extra_args = { "var1": "hello", "var2": "world" }
            ```

            will be subsituted into string "The developer said $var1 $var2".

            When possible, it is preferred that environment varibales be used to subsitute values.
            This is easier to keep track and allows for cleaner code.

        """
        full_file_path = self.HOME_DIR / file_path

        if file_type in ["python", "html", "shell", "file"]:
            file_category = "stringData"

            with open(full_file_path, "r") as f:
                contents: str = f.read()
                templ = Template(contents)
                file_contents = templ.safe_substitute(**extra_args)  # type: ignore

            print(
                f"Rendering {full_file_path} of file_type '{file_type}' using extra_args '{extra_args}'"
            )

        elif file_type == "toml":
            file_category = "data"

            with open(full_file_path, "rb") as f:
                file_contents: dict = tomllib.load(f)  # type: ignore

        elif file_type == "binary":
            file_category = "binaryData"
            with open(full_file_path, "rb") as f:
                file_contents: bytes = f.read()

        else:
            raise ValueError(
                f"Argument file_type of {file_path} needs to be set as python, json, toml, or binary."
            )

        key_name = full_file_path.name

        return {
            key_name: {
                "mountPath": mount_path,
                "mode": int(file_mode, 8),
                file_category: file_contents,
            }
        }
