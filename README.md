# opensciencelab-cluster-v2

## On Stack Deletion

The network load balancer is created via annotaions on a custom k8s service resource. When the CDK stack is destroyed,
the underlying load balancer and networking is not automatically deleted.

> [!WARNNG]
> Before deleting the stack, you MUST open AWS CloudShell and manually run `kubectl -n jupyter delete svc proxy-public-loadbalancer`.

If this is not done, resource deletion will hang and the load balancer and networking will fail deletion.
Then manual cleanup will need to occur and the whole process will take about two hours.

## Architecture

At a high level, the new cluster is a highly customized JupyterHub on EKS, deployed via
a CDK + Actions pipeline.

![Architecture Diagram](docs/OSL%20Cluster%20v2%20Arch%20Diagram.svg)
