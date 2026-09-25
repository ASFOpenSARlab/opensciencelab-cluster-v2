import sys

# The variable "c" is a global variable representing the Config instance.
# This code will be appended to the end of the jupyterhub config.
# Linters like Flake8 often fail to recognize "magic" variables like "c".
# Therefore we apply "noqa: F821"

# Custom Templates
c.JupyterHub.template_paths = ["/usr/local/share/jupyterhub/templates/custom/"]  # noqa: F821

# Custom version endpoint
c.JupyterHub.services = [  # noqa: F821
    {
        # The name determines the URL path: /services/version/
        "name": "version",
        "command": [sys.executable, "/usr/local/etc/jupyterhub/services/version.py"],
        "url": "http://127.0.0.1:8111",
    }
]
