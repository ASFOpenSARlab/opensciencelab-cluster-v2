import os

import boto3

# The variable "c" is a global variable representing the Config instance.
# This code will be appended to the end of the jupyterhub config.
# Linters like Flake8 often fail to recognize "magic" variables like "c".
# Therefore we apply "noqa: F821"

try:
    # If an error occurs with setting the auth but JupyterHub still starts, the dummy login will be the default.
    # This could lead to unauthorized entry. So disable login until the last needed moment.
    print("Disabling login temporarily...")
    c.JupyterHub.authenticator_class = "null"  # noqa: F821

    AWS_REGION = os.environ.get("AWS_REGION", "")
    SSO_TOKEN_ARN = os.environ.get("SSO_TOKEN_ARN", "")
    OPENSARLAB_SSO_TOKEN_PATH = os.environ.get("OPENSARLAB_SSO_TOKEN_PATH", "")
    LAB_PREFIX = os.environ.get("JUPYTERHUB_LAB_PREFIX", "")

    ## Set SSO token to secrets path
    secrets_manager = boto3.client("secretsmanager", region_name=AWS_REGION)
    _sso_token = secrets_manager.get_secret_value(SecretId=SSO_TOKEN_ARN)
    with open(OPENSARLAB_SSO_TOKEN_PATH, "w") as file:
        file.write(_sso_token["SecretString"])

    c.JupyterHub.default_url = f"{LAB_PREFIX}/hub/home"  # noqa: F821

    c.JupyterHub.tornado_settings = {  # noqa: F821
        "cookie_options": {"expires_days": 7.0},
    }

    from jupyterhub.portal_auth import PortalAuthenticator

    c.JupyterHub.authenticator_class = PortalAuthenticator  # noqa: F821

    # How often (seconds) should the JH auth info be refreshed.
    c.Authenticator.auth_refresh_age = 60  # noqa: F821

except Exception as e:
    print(f"Something went wrong with auth... {e}")

finally:
    print("Done with extraConfig::auth.py")
