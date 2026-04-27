import sys
import os

LAB_PREFIX = os.environ.get("JUPYTERHUB_SERVICE_PREFIX", "")

c.JupyterHub.services.append(
    {
        "name": "jupyterhub-idle-culler-service",
        "command": [
            sys.executable,
            "-m",
            "jupyterhub_idle_culler",
            "--timeout=3600",
            "--cull-every=300",
            f"--url=http://127.0.0.1:8081/{LAB_PREFIX}/hub/api",
        ],
    }
)

c.JupyterHub.load_roles.append(
    {
        "name": "jupyterhub-idle-culler-role",
        "scopes": [
            "list:users",
            "read:users:name",
            "read:users:activity",
            "read:servers",
            "delete:servers",
            "admin:servers",
            # "admin:users", # if using --cull-users
        ],
        # assignment of role's permissions to:
        "services": ["jupyterhub-idle-culler-service"],
    }
)
