from aws_cdk import (
    Duration,
    Stack,
    aws_eks_v2 as eks,
    lambda_layer_kubectl_v34,
    aws_iam as iam,
)

from constructs import Construct


class ClusterCdkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        build_role = iam.Role(
            self,
            "ClusterBuildRole",
            assumed_by=iam.ArnPrincipal(
                "arn:aws:iam::233535791844:root"  # Security issue?
            ),
            role_name="us-west-2-eks-cluster-build-role",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AdministratorAccess")
            ],
        )

        cluster_role = iam.Role(
            self,
            "ClusterFullAccess",
            assumed_by=iam.ArnPrincipal(
                "arn:aws:iam::233535791844:root"  # Security issue?
            ),
            role_name="us-west-2-eks-cluster-user-full-access",
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
                                "arn:aws:ssm:us-west-2:233535791844:parameter/*"
                            ],
                            effect=iam.Effect.ALLOW,
                        ),
                    ],
                )
            },
        )

        ## https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_eks/README.html#provisioning-clusters
        cluster = eks.Cluster(
            self,
            "EksCluster",
            cluster_name="eks-cluster",
            version=eks.KubernetesVersion.V1_34,
            kubectl_provider_options=eks.KubectlProviderOptions(
                kubectl_layer=lambda_layer_kubectl_v34.KubectlV34Layer(self, "kubectl"),
            ),
            masters_role=cluster_role,
        )
        cluster.role.grant(
            build_role,
            "eks:*",
        )

        service_account = cluster.add_service_account(
            "EbsCsiServiceAccount",
            name="ebs-csi-controller-sa",
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

        cluster.add_manifest(
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
                "provisioner": "ebs.csi.eks.amazonaws.com",
                "parameters": {
                    "type": "gp3",
                    "fsType": "ext4",
                },
                "allowVolumeExpansion": True,
                "volumeBindingMode": "WaitForFirstConsumer",
            },
        )

        # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_eks/README.html#add-ons
        eks.Addon(
            self,
            "CniAddon",
            addon_name="vpc-cni",
            addon_version="v1.20.4-eksbuild.2",
            cluster=cluster,
            # configuration_values={},
        )
        eks.Addon(  # Check if needed
            self,
            "CoreDnsAddon",
            addon_name="coredns",
            addon_version="v1.12.3-eksbuild.1",
            cluster=cluster,
            # configuration_values={},
        )
        eks.Addon(  # Check if needed
            self,
            "KubeProxyAddon",
            addon_name="kube-proxy",
            addon_version="v1.34.0-eksbuild.2",
            cluster=cluster,
            # configuration_values={},
        )

        # https://artifacthub.io/packages/helm/aws-ebs-csi-driver/aws-ebs-csi-driver
        cluster.add_helm_chart(
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
        cluster.add_helm_chart(
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
                        "tag": "main",
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
