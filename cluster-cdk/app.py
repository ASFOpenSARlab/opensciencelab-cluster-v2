#!/usr/bin/env python3

import os

import aws_cdk as cdk

from cluster_cdk.cluster_cdk_stack import ClusterCdkStack


app = cdk.App()
ClusterCdkStack(
    app,
    f"osl-eks-stack-{os.getenv('DEPLOY_PREFIX', 'UKN')}",
)

app.synth()
