manifest_service_definition = {
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
}

create_namespace_definition = {
    "apiVersion": "v1",
    "kind": "Namespace",
    "metadata": {"name": "jupyter"},
}
