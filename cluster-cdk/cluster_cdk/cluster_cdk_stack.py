import os
import tomllib  # type: ignore
import pathlib

import requests

from aws_cdk import (  # type: ignore
    CfnTag,
    CfnOutput,
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


class ClusterCdkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.setup_env()

        self.setup_networking()

        self.setup_cluster()  # Depends on vpc

        self.setup_nodegroup()  # Depends on cluster

        self.setup_ebs_csi_storage()  # Depends on cluster

        self.setup_jupyterhub()  # Depends on cluster, csi driver, nodegroup

        self.setup_load_balancer()  # Depends on cluster, Jupyter namespace from JupyterHub

        self.setup_outputs()

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

    def setup_env(self):
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

        # All resources in this specific stack will get this tag
        Tags.of(self).add("osl-billing", f"eks-cluster-{self.DEPLOY_PREFIX.lower()}")  # type: ignore

        print(vars(self))

    def setup_networking(self):
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

    def setup_cluster(self) -> None:
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
            role_name=f"eks-cluster-user-full-access-{self.DEPLOY_PREFIX}",
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

        self.user_cloudshell_entry.node.add_dependency(self.cluster)

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

            self.user_access_ui_entry.node.add_dependency(self.cluster)

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

    def setup_nodegroup(self) -> None:
        # https://github.com/aws/aws-cdk/issues/37012eks.Cluster
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
                f"{node['name']}-LaunchTemplate-{self.DEPLOY_PREFIX}",
                launch_template_data=ec2.CfnLaunchTemplate.LaunchTemplateDataProperty(
                    tag_specifications=[
                        ec2.CfnLaunchTemplate.TagSpecificationProperty(
                            resource_type="instance",
                            tags=[
                                CfnTag(key="osl-billing", value=self.DEPLOY_PREFIX),
                                CfnTag(
                                    key="Name",
                                    value=f"jupyterhub-core-{self.DEPLOY_PREFIX}",
                                ),
                            ],
                        ),
                        ec2.CfnLaunchTemplate.TagSpecificationProperty(
                            resource_type="volume",
                            tags=[
                                CfnTag(key="osl-billing", value=self.DEPLOY_PREFIX),
                                CfnTag(
                                    key="Name",
                                    value=f"jupyterhub-core-root-{self.DEPLOY_PREFIX}",
                                ),
                            ],
                        ),
                    ]
                ),
            )

            # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_eks/NodegroupOptions.html
            node_group = self.cluster.add_nodegroup_capacity(
                node["name"],
                nodegroup_name=f"{node['name']}-NodeGroup-{self.DEPLOY_PREFIX}",
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

            if node_type == "core":
                self.core_nodegroup = node_group

    def setup_ebs_csi_storage(self) -> None:
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
            release=f"osl-ebs-driver-{self.DEPLOY_PREFIX.lower()}",  # type: ignore
            namespace="kube-system",
            version=self.csi_driver_version,
            wait=True,
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

    def setup_jupyterhub(self) -> None:
        self.jupyterhub_helm_version = "4.3.2"

        # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_eks/README.html#helm-charts
        # https://artifacthub.io/packages/helm/jupyterhub/jupyterhub?modal=values-schema
        # https://z2jh.jupyter.org/en/latest/resources/reference.html
        self.jupyerhub_helm_chart = self.cluster.add_helm_chart(
            "JupyterhubHelmChart",
            repository="https://jupyterhub.github.io/helm-chart/",
            atomic=False,
            chart="jupyterhub",
            release=f"osl-jupyterhub-{self.DEPLOY_PREFIX.lower()}",  # type: ignore
            version=self.jupyterhub_helm_version,
            namespace="jupyter",
            wait=True,
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
                    "https": {"enabled": False},
                    "service": {
                        "type": "ClusterIP",
                    },
                },
                "custom": {"COST_TAG_KEY": "hello", "COST_TAG_VALUE": "world"},
            },
        )

        self.jupyerhub_helm_chart.node.add_dependency(self.ebs_csi_driver_helm_chart)

    def setup_load_balancer(self) -> None:
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
        # Note that resources are not cleaned up properly on deletion.
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
                        "service.beta.kubernetes.io/aws-load-balancer-name": f"eks-cluster-{self.DEPLOY_PREFIX}",
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
        # And the nlb controller to delete the NLB when the service is deleted.
        # Since such relationships are not explicit in the code above, we need to declare the relationships here.
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

    def setup_outputs(self) -> None:
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
