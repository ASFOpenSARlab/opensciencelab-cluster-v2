#!/usr/bin/env python3
import os

import aws_cdk as cdk

from cluster_cdk.cluster_cdk_stack import ClusterCdkStack


app = cdk.App()
ClusterCdkStack(
    app,
    f"cdk-stack-{os.getenv('LAB_SHORT_NAME', 'UKN')}",
)

app.synth()
