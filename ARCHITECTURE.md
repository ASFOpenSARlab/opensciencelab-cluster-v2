# OpenScienceLab Cluster-V2 Architecture

## JupyterHub

JupyterHub config can be found in the dictionary `jupyterhub_helm_values` in `cluster_cdk_stack.py`

### Idle-Culler

Defined by the `cull` field, the idle culler will remove user servers if they are marked as idle by JupyterHub for `timeout` number of seconds.

JupyterHub marks a server as idle if the users browser tab with their server is closed. This can happen either if the user closes their tab, or if their browser sleeps it. You can read the other factors that contribute to JupyterHub marking a server as idle [in the Jupyter code](https://github.com/jupyter-server/jupyter_server/blob/9a4d6eea2b16815a493b11fffe0b51b1fe55a81b/jupyter_server/serverapp.py#L547-L548).
