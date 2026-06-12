import os
import tomllib  # type: ignore
import pathlib
from string import Template
import json
import re

import requests

from aws_cdk import (  # type: ignore
    CfnTag,
    CfnOutput,
    custom_resources as cr,
    Tags,
    RemovalPolicy,
    Duration,
    Stack,
    SecretValue,
    aws_eks_v2 as eks,
    aws_ec2 as ec2,
    aws_dlm as dlm,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_secretsmanager as secretsmanager,
    aws_events as events,
    aws_events_targets as targets,
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

        self.HOME_DIR = pathlib.Path(__file__).absolute().parent

        # CDK provides the AWS Account number via self.account # "233535791844"
        # CDK provides the AWS Region va self.region

        self.DEPLOY_PREFIX = str(os.getenv("DEPLOY_PREFIX")).lower()
        self.JUPYTER_HUB_DOCKER_TAG = os.getenv(
            "JUPYTER_HUB_DOCKER_TAG", self.DEPLOY_PREFIX
        )
        self.UI_IAM_USER = os.getenv("UI_IAM_USER", None)

        # Default cron schedule to top of every hour
        self.VOLUME_CRON_SCHEDULE = os.getenv("VOLUME_CRON_SCHEDULE", "0 * * * ? *")
        self.SNAPSHOT_WARNING_DAYS = os.getenv("SNAPSHOT_WARNING_DAYS", "5")

        self.LAB_SHORT_NAME = str(os.getenv("LAB_SHORT_NAME", "")).lower()
        if not self.LAB_SHORT_NAME:
            raise Exception("Lab short name is not defined")

        self.ALLOWED_LAB_PROFILES = [
            profile.strip()
            for profile in os.getenv("ALLOWED_LAB_PROFILES", "").split(",")
        ]
        if self.ALLOWED_LAB_PROFILES == [""]:
            raise Exception("Allowed Lab Profiles are not defined")

        self.ADMIN_USERS = [
            username.strip() for username in os.getenv("ADMIN_USERS", "").split(",")
        ]
        if self.ADMIN_USERS == [""]:
            raise Exception("Admin users are not defined")

        self.PORTAL_DOMAINS = os.getenv("PORTAL_DOMAINS", None)
        if not self.PORTAL_DOMAINS:
            raise Exception("Portal domains is not defined")

        self.OPENSCIENCELAB_CONFIG_FILE = self.HOME_DIR / "opensciencelab.toml"

        # Determine the selected lab config values
        self.osl_config = self._get_reduced_osl_config()

        # All resources in this specific stack will get this tag
        Tags.of(self).add("osl-billing", f"eks-cluster-{self.LAB_SHORT_NAME}")  # type: ignore

        self.kubectl_layer = lambda_layer_kubectl_v34.KubectlV34Layer(self, "kubectl")

        print(vars(self))

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
            availability_zones=[f"{self.region}a", f"{self.region}d"],
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
            cluster_name=f"eks-cluster-{self.LAB_SHORT_NAME}",
            version=eks.KubernetesVersion.V1_34,
            kubectl_provider_options=eks.KubectlProviderOptions(
                kubectl_layer=self.kubectl_layer,
            ),
            default_capacity_type=eks.DefaultCapacityType.NODEGROUP,
            default_capacity=0,
        )

        cluster_user_role = iam.Role(
            self,
            "ClusterFullAccess",
            assumed_by=iam.ArnPrincipal(
                f"arn:aws:iam::{self.account}:root"  # Security issue?
            ),
            role_name=f"eks-cluster-user-full-access-{self.LAB_SHORT_NAME}",
            description="IAM Role for user accessing the eks cluster",
            inline_policies={
                "Document1": iam.PolicyDocument(
                    assign_sids=True,
                    statements=[
                        iam.PolicyStatement(
                            actions=[
                                "eks:*",
                                "iam:ListRoles",
                            ],
                            resources=["*"],
                            effect=iam.Effect.ALLOW,
                        ),
                        iam.PolicyStatement(
                            actions=["ssm:GetParameter"],
                            resources=[
                                f"arn:aws:ssm:{self.region}:{self.account}:parameter/*"
                            ],
                            effect=iam.Effect.ALLOW,
                        ),
                    ],
                ),
            },
        )

        ##  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_eks/AccessEntry.html
        self.user_cloudshell_entry = eks.AccessEntry(
            self,
            "UserAccessCloudshell",
            access_policies=[
                eks.AccessPolicy.from_access_policy_name(
                    "AmazonEKSClusterAdminPolicy",
                    access_scope_type=eks.AccessScopeType.CLUSTER,
                ),
            ],
            cluster=self.cluster,
            principal=cluster_user_role.role_arn,
            access_entry_type=eks.AccessEntryType.STANDARD,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # self.user_cloudshell_entry.node.add_dependency(self.cluster)

        if self.UI_IAM_USER:
            # Access Entry for EKS UI
            self.user_access_ui_entry = eks.AccessEntry(
                self,
                "UserAccessUI",
                access_policies=[
                    eks.AccessPolicy.from_access_policy_name(
                        "AmazonEKSClusterAdminPolicy",
                        access_scope_type=eks.AccessScopeType.CLUSTER,
                    ),
                ],
                cluster=self.cluster,
                principal=f"arn:aws:iam::{self.account}:role/aws-reserved/sso.amazonaws.com/{self.UI_IAM_USER}",
                access_entry_type=eks.AccessEntryType.STANDARD,
                removal_policy=RemovalPolicy.DESTROY,
            )

            # self.user_access_ui_entry.node.add_dependency(self.cluster)

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
        for node in self.osl_config["nodes"]:
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

            # Define the Launch Template with the desired EC2 instance tags
            # These tags will be applied to the EC2 instances when they are launched by the Auto Scaling Group
            launch_template = ec2.CfnLaunchTemplate(
                self,
                f"{node['name']}-LaunchTemplate-{self.LAB_SHORT_NAME}",
                launch_template_data=ec2.CfnLaunchTemplate.LaunchTemplateDataProperty(
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
                                    value=f"jupyterhub-{node['name']}-{self.LAB_SHORT_NAME}",
                                ),
                            ],
                        ),
                        ec2.CfnLaunchTemplate.TagSpecificationProperty(
                            resource_type="volume",
                            tags=[
                                CfnTag(key="osl-billing", value=self.LAB_SHORT_NAME),
                                CfnTag(
                                    key="Name",
                                    value=f"jupyterhub-{node['name']}-root-{self.LAB_SHORT_NAME}",
                                ),
                            ],
                        ),
                    ],
                ),
            )

            # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_eks/NodegroupOptions.html
            node_group = self.cluster.add_nodegroup_capacity(
                node["name"],
                nodegroup_name=f"{node['name']}-NodeGroup-{self.LAB_SHORT_NAME}",
                ami_type=eks.NodegroupAmiType.AL2023_X86_64_STANDARD,
                capacity_type=eks.CapacityType.ON_DEMAND,
                desired_size=node.get("group_desired_size", 0),
                max_size=node.get("group_max_size", 100),
                min_size=node.get("group_min_size", 0),
                # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ec2/InstanceClass.html
                instance_types=[
                    ec2.InstanceType(instance) for instance in node["instance"]
                ],
                launch_template_spec=eks.LaunchTemplateSpec(
                    id=launch_template.ref,
                    version=launch_template.attr_latest_version_number,
                ),
                # Force the compute in the public subnet, in a single AZ
                subnets=ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PUBLIC,
                    availability_zones=[f"{self.region}a"],  # Force compute into UW2a
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

            if node_type == "core":
                self._add_policy_from_file(node_group.role, "hub_node_policies.json")

                # Needed so we can make a dependency later
                self.core_nodegroup = node_group

        #####################################################################
        #
        #    Setup EBS CSI Storage for volume creation
        #
        #####################################################################

        # CSI storage
        csi_service_account = self.cluster.add_service_account(
            "EbsCsiServiceAccount",
            name="ebs-csi-controller-sa",
            namespace="kube-system",
            overwrite_service_account=True,
        )
        csi_service_account.role.add_to_principal_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "ec2:AttachVolume",
                    "ec2:CreateSnapshot",
                    "ec2:CreateTags",
                    "ec2:CreateVolume",  # Remove?
                    "ec2:DeleteSnapshot",
                    "ec2:DescribeAvailabilityZones",
                    "ec2:DescribeInstances",
                    "ec2:DescribeSnapshots",
                    "ec2:DescribeTags",
                    "ec2:DescribeVolumeStatus",
                    "ec2:DescribeVolumes",
                    "ec2:DetachVolume",
                    "ec2:ModifyVolume",
                ],
                resources=["*"],
            )
        )

        self.cluster.add_manifest(
            "CsiStorageClass",
            {
                "apiVersion": "storage.k8s.io/v1",
                "kind": "StorageClass",
                "metadata": {
                    "name": "gp3",
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

        self.csi_driver_version = "2.56.1"

        # https://artifacthub.io/packages/helm/aws-ebs-csi-driver/aws-ebs-csi-driver
        self.ebs_csi_driver_helm_chart = self.cluster.add_helm_chart(
            "AwsEbsCsiDriver",
            repository="https://kubernetes-sigs.github.io/aws-ebs-csi-driver",
            atomic=True,
            chart="aws-ebs-csi-driver",
            release=f"osl-ebs-driver-{self.LAB_SHORT_NAME}",  # type: ignore
            namespace="kube-system",
            version=self.csi_driver_version,
            wait=True,
            timeout=Duration.minutes(8),
            values={
                "controller": {
                    "extraCreateMetadata": True,
                    "k8sTagClusterId": self.cluster.cluster_name,
                    "extraVolumeTags": {
                        "osl-billing": self.LAB_SHORT_NAME,
                    },
                    "serviceAccount": {
                        "create": False,
                        "name": csi_service_account.service_account_name,
                    },
                },
            },
        )

        # By being dependecies of the csi driver, they will be created before jupyterhub without any circular dependencies.
        self.ebs_csi_driver_helm_chart.node.add_dependency(self.user_cloudshell_entry)
        if self.UI_IAM_USER:
            self.ebs_csi_driver_helm_chart.node.add_dependency(
                self.user_access_ui_entry
            )

        #####################################################################
        #
        #    Setup JupyterHub
        #
        #####################################################################

        self.jupyterhub_helm_version = "4.3.2"

        jupyterhub_helm_values = {
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
            "singleuser": {
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
                    | self._set_extra_file(
                        "user_server_includes/scripts/pkg_clean.py",
                        "python",
                        "/etc/user_server_includes/scripts/pkg_clean.py",
                    )
                )
            },
            "hub": {
                "image": {
                    "name": "ghcr.io/asfopensarlab/opensciencelab-jupyterhub",
                    "tag": self.JUPYTER_HUB_DOCKER_TAG,
                    "pullPolicy": "Always",
                },
                "db": {
                    "pvc": {
                        "storageClassName": "gp3",
                    }
                },
                "baseUrl": f"/lab/{self.LAB_SHORT_NAME}",
                "config": {
                    "JupyterHub": {
                        "default_url": f"/lab/{self.LAB_SHORT_NAME}/hub/home",
                        "tornado_settings": {
                            "cookie_options": {"expires_days": 7.0},
                        },
                    },
                    "Authenticator": {
                        "admin_users": self.ADMIN_USERS,
                        "auth_refresh_age": 60,
                        "allow_all": True,
                        "enable_auth_state": True,
                    },
                },
                "extraEnv": {
                    "AWS_REGION": self.region,
                    "SSO_TOKEN_ARN": self.sso_token.secret_arn,
                    "SSO_TOKEN_PATH": "/tmp/sso_token",
                    "OPENSARLAB_SSO_TOKEN_PATH": "/tmp/sso_token",
                    "LAB_SHORT_NAME": self.LAB_SHORT_NAME,
                    "JUPYTERHUB_LAB_PREFIX": f"/lab/{self.LAB_SHORT_NAME}",
                    "PORTAL_DOMAINS": self.PORTAL_DOMAINS,
                    "LAB_PROFILES": json.dumps(self.osl_config["lab_profiles"]),
                },
                "extraFiles": (
                    {}
                    | self._set_extra_file(
                        "jupyterhub/portal_auth.py",
                        "python",
                        "/usr/local/lib/python3.12/site-packages/jupyterhub/portal_auth.py",
                    )
                    | self._set_extra_file(
                        "jupyterhub/config.d/1_auth.py",
                        "python",
                        "/usr/local/etc/jupyterhub/jupyterhub_config.d/1_auth.py",
                    )
                    | self._set_extra_file(
                        "jupyterhub/config.d/0_extras.py",
                        "python",
                        "/usr/local/etc/jupyterhub/jupyterhub_config.d/0_extras.py",
                    )
                    | self._set_extra_file(
                        "jupyterhub/hub_home.html.j2",
                        "html",
                        "/usr/local/share/jupyterhub/templates/custom/page.html",
                    )
                    | self._set_extra_file(
                        "jupyterhub/config.d/2_profiles.py",
                        "python",
                        "/usr/local/etc/jupyterhub/jupyterhub_config.d/2_profiles.py",
                    )
                ),
            },
            "proxy": {
                "https": {"enabled": False},
                "service": {
                    "type": "ClusterIP",
                },
            },
            "custom": {"COST_TAG_KEY": "hello", "COST_TAG_VALUE": "world"},
        }

        # print(json.dumps(jupyterhub_helm_values))

        # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_eks/README.html#helm-charts
        # https://artifacthub.io/packages/helm/jupyterhub/jupyterhub?modal=values-schema
        # https://z2jh.jupyter.org/en/latest/resources/reference.html
        self.jupyerhub_helm_chart = self.cluster.add_helm_chart(
            "JupyterhubHelmChart",
            repository="https://jupyterhub.github.io/helm-chart/",
            atomic=False,
            chart="jupyterhub",
            release=f"osl-jupyterhub-{self.LAB_SHORT_NAME}",  # type: ignore
            version=self.jupyterhub_helm_version,
            namespace="jupyter",
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
                "clusterName": self.cluster.cluster_name,
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
                    "namespace": "jupyter",
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
            namespace="jupyter",
            timeout=Duration.minutes(15),
        )

        #####################################################################
        #
        #    Volume/Snapshot Management
        #
        #####################################################################

        self.volume_management_lambda = lambda_.Function(
            self,
            description=f"{self.DEPLOY_PREFIX} Volume Management Lambda",
            id=f"{self.DEPLOY_PREFIX}_volume_management",
            runtime=lambda_.Runtime.PYTHON_3_13,
            timeout=Duration.minutes(15),
            handler="volume_management.lambda_handler",
            code=lambda_.Code.from_asset(
                path="cluster_cdk/lambdas/",
            ),
            environment={
                "CLUSTER_NAME": self.cluster.cluster_name,
                "SNAPSHOT_WARNING_DAYS": self.SNAPSHOT_WARNING_DAYS,
                "PORTAL_DOMAINS": self.PORTAL_DOMAINS,
                "SSO_SECRET": self.sso_token.secret_arn,
            },
        )

        self.requirements_layer = lambda_.LayerVersion(
            self,
            "RequirementsLayer",
            # /tmp/.build/lambda/ is make in the Makefile @ bundle-deps
            code=lambda_.Code.from_asset("/tmp/.build/lambda/"),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_13],
        )

        # Add kubectl, awscli, and requirements lambda layers
        self.volume_management_lambda.add_layers(self.kubectl_layer)
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

        # grant lambda the access it needs
        self.sso_token.grant_read(self.volume_management_lambda)
        self.volume_management_lambda.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "ec2:DescribeVolumes",
                    "ec2:DescribeSnapshots",
                    "ec2:CreateSnapshot",
                    "ec2:DeleteSnapshot",
                ],
                resources=["*"],
            )
        )

        self.cron_schedule_rule = events.Rule(
            self,
            id=f"{self.DEPLOY_PREFIX}_volume_management_rule",
            schedule=events.Schedule.expression(f"cron({self.VOLUME_CRON_SCHEDULE})"),
            description="Triggers Volume Management Lambda",
        )

        self.cron_schedule_rule.add_target(
            targets.LambdaFunction(self.volume_management_lambda)
        )

        # DLM configuration to create daily volume snapshots
        self.dlm_role = iam.Role(
            self,
            id=f"{self.DEPLOY_PREFIX}_dlm_service_role",
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
            id=f"{self.DEPLOY_PREFIX}_daily_snapshot",
            description="Daily backup policy for EBS volumes",
            execution_role_arn=self.dlm_role.role_arn,
            state="ENABLED",
            policy_details=dlm.CfnLifecyclePolicy.PolicyDetailsProperty(
                resource_types=["VOLUME"],
                # Target volumes from this cluster only
                target_tags=[
                    CfnTag(key="KubernetesCluster", value=self.cluster.cluster_name),
                ],
                schedules=[
                    dlm.CfnLifecyclePolicy.ScheduleProperty(
                        name="DailySnapshots",
                        tags_to_add=[
                            CfnTag(
                                key="CreatedBy",
                                value=f"{self.DEPLOY_PREFIX}_daily_snapshot",
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

    def _get_reduced_osl_config(self) -> dict:
        """
        Return a subset of profiles and nodes found in opensciencelab.toml based on list of lab profiles given in GitHub env.

        Also include required nodes (like core) that don't match for any particular profile.

        """
        with open(self.OPENSCIENCELAB_CONFIG_FILE, "rb") as f:
            osl_config: dict = tomllib.load(f)

        possible_profiles = osl_config.get("lab_profiles", None)
        if not possible_profiles:
            raise Exception("No lab profiles found in the osl toml config")

        all_nodes = osl_config.get("nodes", None)
        if not all_nodes:
            raise Exception("No nodes found in the osl toml config")

        # Put config data into a format better for code interactions
        # { "name": "hello", "attr": "value", ... }
        possible_profiles = [
            {"name": name} | body for name, body in possible_profiles.items()
        ]

        all_nodes = [{"name": name} | body for name, body in all_nodes.items()]

        desired_profiles = []
        desired_nodes = []

        for profile in possible_profiles:
            if profile["name"] in self.ALLOWED_LAB_PROFILES:
                desired_profiles.append(profile)

                # See if there is a proper node configuration for the profile
                node_for_profile = None
                for node_body in all_nodes:
                    if profile["node"] == node_body["name"]:
                        node_for_profile = node_body

                if not node_for_profile:
                    raise Exception(
                        f"Desired lab profile name '{profile['name']}' for '{self.LAB_SHORT_NAME}' does not have a valid node assigned."
                    )

                desired_nodes.append(node_for_profile)

            else:
                print(
                    f"Desired lab profile name '{profile['name']}' for '{self.LAB_SHORT_NAME}' does not match any selected profile names."
                )

        # Add any required nodes (like core)
        for node in all_nodes:
            if node.get("required", False):
                desired_nodes.append(node)

        # Get rid of duplicates
        desired_profiles = [
            profile
            for n, profile in enumerate(desired_profiles)
            if desired_profiles.index(profile) == n
        ]
        desired_nodes = [
            node
            for n, node in enumerate(desired_nodes)
            if desired_nodes.index(node) == n
        ]

        return {"lab_profiles": desired_profiles, "nodes": desired_nodes}

    def _add_policy_from_file(self, the_role: iam.Role, file_name: str) -> None:
        """
        Predefined roles sometimes need addtional custom policies applied (especially for node roles).
        This method attaches a policy defined in a specially formatted file.

        The policy in the files must be in one of two formats: a json list

        ```
            [
                {
                    "Sid": "MySid",
                    "Effect": "Allow",
                    "Action": [
                        "ec2:DescribeSnapshots",
                        "ec2:CreateVolume",
                        "ec2:CreateTags"
                    ],
                    "Resource": "*"
                },
                {
                    "Sid": "AnotherSid",
                    "Effect": "Allow",
                    "Action": [
                        "ec2:DescribeVolumes",
                        "ec2:CreateTags"
                    ],
                    "Resource": "*"
                }
            ]
        ```

        or just json

        ```
            {
                "Sid": "MySid",
                "Effect": "Allow",
                "Action": [
                    "ec2:DescribeSnapshots",
                    "ec2:CreateVolume",
                    "ec2:CreateTags"
                ],
                "Resource": "*"
            }
        ```
        """

        with open(self.HOME_DIR / "manifests/policies" / pathlib.Path(file_name)) as f:
            policy_data: dict | list = json.load(f)

        if isinstance(policy_data, list):
            for policy in policy_data:
                the_role.add_to_policy(iam.PolicyStatement.from_json(policy))

        elif isinstance(policy_data, dict):
            the_role.add_to_policy(iam.PolicyStatement.from_json(policy_data))

        else:
            print(f"Policy for {file_name} in wrong format?")

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
