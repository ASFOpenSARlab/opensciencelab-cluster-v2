import os
import tomllib  # type: ignore
import pathlib

from aws_cdk import (  # type: ignore
    CfnTag,
    Tags,
    RemovalPolicy,
    Duration,
    Stack,
    aws_eks_v2 as eks,
    aws_ec2 as ec2,
    aws_iam as iam,
    lambda_layer_kubectl_v34,
)

from constructs import Construct  # type: ignore

from .manifests import csi_storage_class


class ClusterCdkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # CDK provides the AWS Account number via self.account # "233535791844"
        # CDK provides the AWS Region va self.region
        self.DEPLOY_PREFIX = os.getenv("DEPLOY_PREFIX")
        self.JUPYTER_HUB_DOCKER_TAG = os.getenv(
            "JUPYTER_HUB_DOCKER_TAG", self.DEPLOY_PREFIX
        )
        self.UI_IAM_USER = os.getenv("UI_IAM_USER", None)

        self.OPENSCIENCELAB_CONFIG_FILE = (
            pathlib.Path(__file__).absolute().parent / "opensciencelab.toml"
        )
        osl_config_with_defaults = self._get_osl_config_with_defaults()

        # If deploy_prefix not found in config sections, use defaults
        # This allows for development using defaults
        self.osl_config = osl_config_with_defaults.get(
            self.DEPLOY_PREFIX, osl_config_with_defaults.get("defaults", {})
        )

        print(vars(self))

        # All resources in this specific stack will get this tag
        Tags.of(self).add("osl-billing", self.DEPLOY_PREFIX.lower())  # type: ignore

        # Two subnets for EKS
        self.public_subnet = ec2.SubnetConfiguration(
            name="PublicSubnet",
            subnet_type=ec2.SubnetType.PUBLIC,
            cidr_mask=24,
        )
        self.private_subnet = ec2.SubnetConfiguration(
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
            subnet_configuration=[self.public_subnet, self.private_subnet],
        )

        ## https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_eks_v2/README.html#provisioning-clusters
        self.cluster = eks.Cluster(
            self,
            "EksCluster",
            vpc=self.vpc,
            cluster_name=f"eks-cluster-{self.DEPLOY_PREFIX}",
            version=eks.KubernetesVersion.V1_34,
            kubectl_provider_options=eks.KubectlProviderOptions(
                kubectl_layer=lambda_layer_kubectl_v34.KubectlV34Layer(self, "kubectl"),
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
            role_name=f"{self.region}-{self.DEPLOY_PREFIX}-eks-cluster-user-full-access",
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
        eks.AccessEntry(
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

        if self.UI_IAM_USER:
            # Access Entry for EKS UI
            eks.AccessEntry(
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

        # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_eks/AlbController.html
        # This installs the load balancer helm chart and needed networking.
        # However, the actual load balancer and traffic paths are described in jupyterhub proxy service annotations
        # Addtional helm chart options:https://github.com/kubernetes-sigs/aws-load-balancer-controller/blob/main/helm/aws-load-balancer-controller/values.yaml
        eks.AlbController(
            self,
            "MyAlbController",
            cluster=self.cluster,
            version=eks.AlbControllerVersion.V2_8_2,
            additional_helm_chart_values={
                "enableWaf": False,
                "enableWafv2": False,
                "defaultTags": {"AlbControllerManaged": True},
            },
            removal_policy=RemovalPolicy.DESTROY,
        )

        # https://github.com/aws/aws-cdk/issues/37012
        for node in self.osl_config["nodes"]:
            node_type = node.get("node_type", "user")

            # Node labels to apply depending on node type
            node_labels = node.get("labels", {})
            if node_type == "core":
                node_labels["hub.jupyter.org/node-purpose"] = "core"
                node_labels["opensciencelab.local/node-type"] = "core"
            if node_type == "user":
                node_labels["hub.jupyter.org/node-purpose"] = "user"
                node_labels["opensciencelab.local/node-type"] = "user"

            # Define the Launch Template with the desired EC2 instance tags
            # These tags will be applied to the EC2 instances when they are launched by the Auto Scaling Group
            launch_template = ec2.CfnLaunchTemplate(
                self,
                f"{self.DEPLOY_PREFIX}-{node['name']}-LaunchTemplate",
                launch_template_data=ec2.CfnLaunchTemplate.LaunchTemplateDataProperty(
                    tag_specifications=[
                        ec2.CfnLaunchTemplate.TagSpecificationProperty(
                            resource_type="instance",
                            tags=[
                                CfnTag(key="osl-billing", value=self.DEPLOY_PREFIX),
                                CfnTag(key="Name", value=f"{self.DEPLOY_PREFIX}-core"),
                            ],
                        ),
                        ec2.CfnLaunchTemplate.TagSpecificationProperty(
                            resource_type="volume",
                            tags=[
                                CfnTag(key="osl-billing", value=self.DEPLOY_PREFIX),
                                CfnTag(
                                    key="Name", value=f"{self.DEPLOY_PREFIX}-core-root"
                                ),
                            ],
                        ),
                    ]
                ),
            )

            node_group = self.cluster.add_nodegroup_capacity(
                node["name"],
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

            # SMCE required observability policies
            for managed_policy in [
                "AmazonSSMManagedInstanceCore",
                "CloudWatchAgentAdminPolicy",
                "CloudWatchAgentServerPolicy",
            ]:
                node_group.role.add_managed_policy(
                    iam.ManagedPolicy.from_aws_managed_policy_name(managed_policy)
                )

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
            "CsiStorageClass", csi_storage_class.manifest_definition
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
        eks.Addon(
            self,
            "CloudwatchObserv",
            addon_name="amazon-cloudwatch-observability",
            addon_version="v4.10.2-eksbuild.1",
            cluster=self.cluster,
        )

        eks.Addon(  # Check if needed
            self,
            "KubeProxyAddon",
            addon_name="kube-proxy",
            addon_version="v1.34.0-eksbuild.2",
            cluster=self.cluster,
            # configuration_values={},
        )

        # https://artifacthub.io/packages/helm/aws-ebs-csi-driver/aws-ebs-csi-driver
        self.cluster.add_helm_chart(
            "AwsEbsCsiDriver",
            repository="https://kubernetes-sigs.github.io/aws-ebs-csi-driver",
            atomic=True,
            chart="aws-ebs-csi-driver",
            release=f"osl-ebs-driver-{self.DEPLOY_PREFIX.lower()}",  # type: ignore
            namespace="kube-system",
            version="2.56.1",
            timeout=Duration.minutes(8),
            values={
                "controller": {
                    "extraCreateMetadata": True,
                    "k8sTagClusterId": self.cluster.cluster_name,
                    "extraVolumeTags": {
                        "osl-billing": self.DEPLOY_PREFIX,
                    },
                    "serviceAccount": {
                        "create": False,
                        "name": csi_service_account.service_account_name,
                    },
                },
            },
        )

        # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_eks/README.html#helm-charts
        # https://artifacthub.io/packages/helm/jupyterhub/jupyterhub?modal=values-schema
        # https://z2jh.jupyter.org/en/latest/resources/reference.html
        self.cluster.add_helm_chart(
            "JupyterhubHelmChart",
            repository="https://jupyterhub.github.io/helm-chart/",
            atomic=False,
            chart="jupyterhub",
            release=f"osl-jupyterhub-{self.DEPLOY_PREFIX.lower()}",  # type: ignore
            version="4.3.2",
            namespace="jupyter",
            timeout=Duration.minutes(10),
            values={
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
                "hub": {
                    "image": {
                        "name": "ghcr.io/asfopensarlab/opensciencelab-cluster-v2/cluster/jupyterhub",
                        "tag": self.JUPYTER_HUB_DOCKER_TAG,
                        "pullPolicy": "Always",
                    },
                    "db": {
                        "pvc": {
                            "storageClassName": "gp3",
                        }
                    },
                },
                "proxy": {
                    "service": {
                        "type": "LoadBalancer",
                        "nodePorts": {
                            "http": 30052,
                        },
                        "annotations": {
                            "service.beta.kubernetes.io/aws-load-balancer-type": "external",
                            "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type": "ip",
                            "service.beta.kubernetes.io/aws-load-balancer-scheme": "internet-facing",
                        },
                    },
                },
                "custom": {"COST_TAG_KEY": "hello", "COST_TAG_VALUE": "world"},
            },
        )

    def _get_osl_config_with_defaults(self) -> dict:
        with open(self.OPENSCIENCELAB_CONFIG_FILE, "rb") as f:
            config: dict = tomllib.load(f)

        defaults: dict = config.get("defaults", {})

        merged = {}

        merged["defaults"] = defaults

        # Cycle through all the labs
        for lab_name, lab_config in config.items():
            lab = {}

            lab["environment"] = lab_config.get("environment", defaults["environment"])

            # Replace of all nodes if lab nodes are defined
            lab["nodes"] = lab_config.get("nodes", defaults["nodes"])

            # Replace of all nodes if lab nodes are defined
            lab["lab_profiles"] = lab_config.get(
                "lab_profiles", defaults["lab_profiles"]
            )

            merged[lab_name] = lab

        return merged
