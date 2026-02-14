import aws_cdk as core
import aws_cdk.assertions as assertions

from cluster_cdk.cluster_cdk_stack import ClusterCdkStack


# example tests. To run these tests, uncomment this file along with the example
# resource in cluster_cdk/cluster_cdk_stack.py
def test_sqs_queue_created():
    app = core.App()
    stack = ClusterCdkStack(app, "cluster-cdk")
    template = assertions.Template.from_stack(stack)

    # print so linter is happy
    print(template)


#     template.has_resource_properties("AWS::SQS::Queue", {
#         "VisibilityTimeout": 300
#     })
