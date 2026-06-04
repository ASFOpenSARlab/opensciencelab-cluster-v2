#!/bin/bash
set -ve

# Add Path to local pip execs.
export PATH=$HOME/.local/bin:$PATH

# Copy over extension override
cp /etc/user_server_includes/overrides/default.json /opt/conda/share/jupyter/lab/settings/overrides.json

# Disable default Jupyterlab-tours tours
jupyter labextension disable "jupyterlab-tour:notebook-tours"
jupyter labextension disable "jupyterlab-tour:default-tours"

# Disable the extension manager in Jupyterlab since server extensions are uninstallable
# by users and non-server extension installs do not persist over server restarts
jupyter labextension disable @jupyterlab/extensionmanager-extension

# Disable proxy of virtual desktop with shortcuts. One might be able to get the desktop still via url /desktop.
jupyter labextension disable @jupyterhub/jupyter-server-proxy

CONDARC=$HOME/.condarc
if ! test -f "$CONDARC"; then
	cat <<EOT >>"$CONDARC"
envs_dirs:
  - /home/jovyan/.local/envs
  - /opt/conda/envs
EOT
fi

eval "$(mamba shell hook --shell bash)"
