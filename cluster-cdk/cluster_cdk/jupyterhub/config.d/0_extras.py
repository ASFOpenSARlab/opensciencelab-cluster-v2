# The variable "c" is a global variable representing the Config instance.
# This code will be appended to the end of the jupyterhub config.
# Linters like Flake8 often fail to recognize "magic" variables like "c".
# Therefore we apply "noqa: F821"

# Custom Templates
c.JupyterHub.template_paths = ["/usr/local/share/jupyterhub/templates/custom/"]  # noqa: F821
