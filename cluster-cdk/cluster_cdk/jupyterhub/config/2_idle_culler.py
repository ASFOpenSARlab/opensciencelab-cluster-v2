import sys

c.JupyterHub.services.append(
    {
        "name": "jupyterhub-idle-culler-service",
        "command": [
            sys.executable,
            "-m",
            "jupyterhub_idle_culler",
            "--timeout=3600",
            "--cull-every=300",
            "--url=http://127.0.0.1:8081/lab/$LAB_SHORT_NAME/hub/api",
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
