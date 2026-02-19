from aws_cdk import (
    # Duration,
    Stack,
    # aws_sqs as sqs,
    aws_eks as eks,
)
from aws_cdk.lambda_layer_kubectl_v34 import KubectlV34Layer
from constructs import Construct


class ClusterCdkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # The code that defines your stack goes here

        # example resource
        # queue = sqs.Queue(
        #     self, "ClusterCdkQueue",
        #     visibility_timeout=Duration.seconds(300),
        # )
        cluster = eks.Cluster(
            "EksCluster",
            cluster_name="eks-cluster",
            version=eks.KubernetesVersion.V1_34,
            kubectl_layer=KubectlV34Layer,
            scope=scope,
        )
        
        cluster.add_helm_chart(
            "JupyterHub",
            version="4.3.2",
        )
        
