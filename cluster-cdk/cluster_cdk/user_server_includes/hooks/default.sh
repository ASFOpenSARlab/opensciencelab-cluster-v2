#!/bin/bash
set -ve

# Sleep for 30 seconds and hope that the Istio proxy will be done setting up.
# sleep 30

# Add Path to local pip execs.
export PATH=$HOME/.local/bin:$PATH

python /etc/user_server_includes/scripts/pkg_clean.py

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

# IS THIS NEEDED ANYMORE?
#
# CONDARC=$HOME/.condarc
# if ! test -f "$CONDARC"; then
# 	cat <<EOT >>"$CONDARC"
# channels:
#   - conda-forge
#   - defaults
#
# channel_priority: strict
#
# envs_dirs:
#   - /home/jovyan/.local/envs
#   - /opt/conda/envs
# EOT
# fi

# KERNELS=$HOME/.local/share/jupyter/kernels
# OLD_KERNELS=$HOME/.local/share/jupyter/kernels_old
# FLAG=$HOME/.jupyter/old_kernels_flag.txt
# if ! test -f "$FLAG" && test -d "$KERNELS"; then
# 	cp /etc/user_server_includes/etc/old_kernels_flag.txt "$HOME/.jupyter/old_kernels_flag.txt"
# 	mv "$KERNELS" "$OLD_KERNELS"
# 	cp /etc/user_server_includes/etc/kernels_rename_README "$OLD_KERNELS/kernels_rename_README"
# fi

# # Remove CondaKernelSpecManager section from jupyter_notebook_config.json to display full kernel names
# # We can do this now since jlab4 dynamically expands launcher buttons to fit
# JN_CONFIG=$HOME/.jupyter/jupyter_notebook_config.json
# if test -f "$JN_CONFIG" && jq -e '.CondaKernelSpecManager' "$JN_CONFIG" &>/dev/null; then
# 	jq 'del(.CondaKernelSpecManager)' "$JN_CONFIG" >temp && mv temp "$JN_CONFIG"
# fi

# conda init

BASH_PROFILE=$HOME/.bash_profile
if ! test -f "$BASH_PROFILE"; then
	cat <<EOT >>"$BASH_PROFILE"
if [ -s ~/.bashrc ]; then
    source ~/.bashrc;
fi
EOT
fi
