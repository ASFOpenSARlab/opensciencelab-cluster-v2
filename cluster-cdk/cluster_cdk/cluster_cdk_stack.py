import os

from aws_cdk import (
    Duration,
    Stack,
    aws_ec2 as ec2,
    aws_eks_v2 as eks,
    aws_iam as iam,
    lambda_layer_kubectl_v34,
)

from .manifests import csi_storage_class

from constructs import Construct

# SMCE required observability policies
SMCE_POLICIES = [
    "AmazonSSMManagedInstanceCore",
    "CloudWatchAgentAdminPolicy",
    "CloudWatchAgentServerPolicy",
]


class ClusterCdkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.DEPLOY_PREFIX = os.getenv("DEPLOY_PREFIX")
        self.JUPYTER_HUB_DOCKER_TAG = os.getenv("JUPYTER_HUB_DOCKER_TAG")
        self.EKS_NODE_TYPE = os.getenv("EKS_NODE_TYPE")

        cluster_role = iam.Role(
            self,
            "ClusterFullAccess",
            assumed_by=iam.ArnPrincipal(
                f"arn:aws:iam::{self.account}:root"  # Security issue?
            ),
            role_name=f"{self.region}-{self.DEPLOY_PREFIX}-eks-cluster-user-full-access",
            description="IAM Role for accessing the eks cluster",
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
                )
            },
        )

        ## https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_eks_v2/README.html#provisioning-clusters
        self.cluster = eks.Cluster(
            self,
            "EksCluster",
            cluster_name=f"eks-cluster-{self.DEPLOY_PREFIX}",
            version=eks.KubernetesVersion.V1_34,
            kubectl_provider_options=eks.KubectlProviderOptions(
                kubectl_layer=lambda_layer_kubectl_v34.KubectlV34Layer(self, "kubectl"),
            ),
            masters_role=cluster_role,
            default_capacity_type=eks.DefaultCapacityType.NODEGROUP,
            default_capacity=0,
        )

        # https://github.com/aws/aws-cdk/issues/37012
        self.cluster.add_nodegroup_capacity(
            "NodeGroupOverride",
            ami_type=eks.NodegroupAmiType.AL2023_X86_64_STANDARD,
            capacity_type=eks.CapacityType.ON_DEMAND,
            desired_size=2,
            max_size=5,
            min_size=0,
            # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ec2/InstanceClass.html
            instance_types=[
                ec2.InstanceType(self.EKS_NODE_TYPE),
            ],
        )


        ##  Grab the node role, and attach SMCE Policies.
        # NOTE: EksClusternodePoolRole will need to change to f"{ClusterId}nodePoolRole" if
        # self.cluster's id is changed from "EksCluster"
        self.node_role = self._find_node_role_by_id(node_id="NodeGroupRole")
        if self.node_role:
            self._attach_role_policies(self.node_role)
        else:
            print("Could not attach policies to EksClusternodePoolRole")

        service_account = self.cluster.add_service_account(
            "EbsCsiServiceAccount",
            name=f"{self.DEPLOY_PREFIX}-ebs-csi-controller-sa",
            namespace="kube-system",
            overwrite_service_account=True,
        )
        service_account.role.add_to_principal_policy(
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

        self.cluster.add_manifest("CsiStorageClass", csi_storage_class.manifest_definition)

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
            namespace="kube-system",
            version="2.56.1",
            timeout=Duration.minutes(8),
            values={
                "controller": {
                    "extraCreateMetadata": True,
                    "k8sTagClusterId": "eks-cluster",
                    # "extraVolumeTags": { # For cost tracking per cluster?
                    #     "hello": "world"
                    # },
                    "serviceAccount": {
                        "create": False,
                        "name": service_account.service_account_name,
                    },
                },
            },
        )

        ## https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_eks/README.html#helm-charts
        ## https://artifacthub.io/packages/helm/jupyterhub/jupyterhub?modal=values-schema
        ## https://z2jh.jupyter.org/en/latest/resources/reference.html
        self.cluster.add_helm_chart(
            "JupyterhubHelmChart",
            repository="https://jupyterhub.github.io/helm-chart/",
            atomic=True,
            chart="jupyterhub",
            version="4.3.2",
            namespace="jupyter",
            timeout=Duration.minutes(15),
            values={
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

    def _find_node_role_by_id(self, node_id):
        for child in self.cluster.node.find_all():
            if isinstance(child, iam.Role):
                if node_id in child.node.id:
                    return child
                # else:
                #     print(f"Found unrelated role: {child.node.id}")

    def _attach_role_policies(self, role):
        for policy_name in SMCE_POLICIES:
            role.add_managed_policy(
                iam.ManagedPolicy.from_aws_managed_policy_name(policy_name)
            )
