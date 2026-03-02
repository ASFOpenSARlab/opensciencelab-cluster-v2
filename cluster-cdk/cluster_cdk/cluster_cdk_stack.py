from aws_cdk import (
    Duration,
    Stack,
    # aws_sqs as sqs,
    aws_ec2 as ec2,
    aws_eks_v2_alpha as eks,
    lambda_layer_kubectl_v34,
    aws_iam as iam,
)
from constructs import Construct


class ClusterCdkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # vpc = ec2.Vpc(
        #     self,
        #     "Vpc"
        # )
        
        
        cluster_role = iam.Role(
            self,
            "ClusterFullAccess",
            assumed_by=iam.ArnPrincipal("arn:aws:iam::233535791844:root"), # Security issue?
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
                            actions=[
                                "ssm:GetParameter"
                            ],
                            resources=["arn:aws:ssm:us-west-2:233535791844:parameter/*"],
                            effect=iam.Effect.ALLOW,
                        ),
                    ]
                )
            }
        )
        
        # service_account_role = 

        ## https://constructs.dev/packages/@aws-cdk/aws-eks-v2-alpha/v/2.238.0-alpha.0/api/Cluster?lang=python
        cluster = eks.Cluster(
            self,
            "EksCluster",
            cluster_name="eks-cluster",
            version=eks.KubernetesVersion.V1_34,
            kubectl_provider_options=eks.KubectlProviderOptions(
                kubectl_layer=lambda_layer_kubectl_v34.KubectlV34Layer(self, "kubectl"),
            ),
            masters_role=cluster_role,
            # role=cluster_role,
            # vpc=vpc,
        )
        
        
        ### LOOK INTO THIS
            ### 
        service_account = cluster.add_service_account(
            "EbsCsiServiceAccount",
            name="ebs-csi-service-account",
        )
        service_account.role.add_to_principal_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "ec2:CreateSnapshot",
                    "ec2:AttachVolume",
                    "ec2:DetachVolume",
                    "ec2:ModifyVolume",
                    "ec2:DescribeAvailabilityZones",
                    "ec2:DescribeInstances",
                    "ec2:DescribeSnapshots",
                    "ec2:DescribeTags",
                    "ec2:DescribeVolumes",
                    "ec2:DescribeVolumeStatus",
                    "ec2:DeleteSnapshot",
                    "ec2:CreateTags"
                ],
                resources=["*"],
                )
        )
        # service_account.role.add_managed_policy(
        #     iam.ManagedPolicy.#.from_aws_managed_policy_name("AmazonEBSCSIDriverPolicy")
        # )
        
        # cluster_role.add_to_policy(
        #     iam.PolicyStatement(
        #         actions=["eks:AccessKubernetesApi", "eks:Describe*", "eks:List*"],
        #         resources=[cluster.cluster_arn],
        #     )
        # )
        
#         cluster.add_manifest(
#             "AwsAuth",
#             {
#                 "apiVersion": "v1",
#                 "kind": "ConfigMap",
#                 "metadata": {
#                     "name": "aws-auth",
#                     "namespace": "kube-system",
#                 },
#                 "data":{
#                     "mapRoles": """
# - rolearn: arn:aws:iam::233535791844:role/us-west-2-eks-cluster-user-full-access
#     username: cluster-user-full-access
#     groups:
#     - system:masters
#                     """
#                 },
#             }
#         )
        
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
                "provisioner": "ebs.csi.aws.com",
                "parameters": {
                    "type": "gp3",
                    "fsType": "ext4",
                },
                "allowVolumeExpansion": True,
                "volumeBindingMode": "Immediate",
            }
        )
        
        # https://artifacthub.io/packages/helm/aws-ebs-csi-driver/aws-ebs-csi-driver
        cluster.add_helm_chart(
            "AwsEbsCsiDriver",
            repository="https://kubernetes-sigs.github.io/aws-ebs-csi-driver",
            # atomic=True,
            chart="aws-ebs-csi-driver",
            namespace="kube-system",
            version="2.56.1",
            # timeout=Duration.minutes(6),
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
                    }
                }
            }
        )
        
        ## https://constructs.dev/packages/@aws-cdk/aws-eks-v2-alpha/v/2.238.0-alpha.0/api/Cluster?lang=python#addHelmChart
        cluster.add_helm_chart(
            "JupyterhubHelmChart",
            repository="https://jupyterhub.github.io/helm-chart/",
            # atomic=True,
            chart="jupyterhub",
            version="4.3.2",
            namespace="jupyter",
            # timeout=Duration.minutes(10),
            values={
                "hub":{
                    "db": {
                        "pvc":{
                            "storageClassName": "gp3"
                        }
                    }
                },
                "custom": {
                    "COST_TAG_KEY": "hello",
                    "COST_TAG_VALUE": "world"
                }
            },
        )
        
