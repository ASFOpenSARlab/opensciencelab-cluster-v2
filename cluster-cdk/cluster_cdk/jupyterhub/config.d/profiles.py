from typing import List, Dict
import logging
import json
import traceback
import re
import os
import urllib

from tornado.httpclient import AsyncHTTPClient, HTTPResponse

from opensarlab.auth import encryptedjwt

# What is happening here
LAB_PROFILES: list = json.loads(os.environ.get("LAB_PROFILES", "[]"))
LAB_SHORT_NAME: str | None = os.environ.get("LAB_SHORT_NAME", None)
PORTAL_DOMAINS: str = os.environ.get("PORTAL_DOMAINS", "")


class My401Exception(Exception):
    pass


class MyErrorException(Exception):
    pass


async def _get_portal_host(auth_state: dict) -> str:
    # Check to see if portal return path
    if auth_state:
        return_portal = auth_state.get("return_portal")
        if return_portal:
            if return_portal not in PORTAL_DOMAINS.split(","):
                logging.fatal(
                    "Portal %s not in approved domains %s",
                    return_portal,
                    PORTAL_DOMAINS,
                )
            else:
                return f"https://{return_portal}"
        else:
            logging.warning("return_portal not in %s", auth_state)
    else:
        logging.debug("auth_state not provided")

    primary_portal_domain = PORTAL_DOMAINS.split(",")[0].strip()

    return primary_portal_domain


async def _get_data_from_auth_api(username: str, portal_domain: str) -> dict:
    try:
        body = json.dumps({"username": f"{username}"})

        response: HTTPResponse = await AsyncHTTPClient().fetch(
            f"{portal_domain}/portal/hub/auth", body=body, method="POST"
        )

        if not response.code == 200:
            logging.error(
                f"Auth response code is not 200. Code: {response.code}, {response}"
            )
            raise MyErrorException()

        response_dict: dict = json.loads(response.body)
        if "ERROR" in response_dict["message"]:
            logging.error(f"{response_dict['message']}")
            raise MyErrorException()

    except Exception as e:
        logging.error(f"Something went wrong with retrieving authentication. {e}")
        raise My401Exception()

    try:
        jwt_data: dict = encryptedjwt.decrypt(response_dict["data"])  # type: ignore
    except Exception as e:
        logging.error(f"Profiles.py JWT decryption went wrong: {e}")
        raise MyErrorException()

    return jwt_data


async def lab_profile_list_hook(spawner: c.Spawner) -> List[Dict]:  # noqa: F821
    # If nothing has been assigned to the user, create a dummy no_profiles option for the default.
    # This will attempt to find a "no_profiles" node to spin up and obviously fail.
    # Otherwise, the default profile is to spin up a basic jupyterlab server on a randomly selected node.
    def no_profiles():
        return [
            {
                "display_name": "No Profiles",
                "slug": "noprofiles",
                "description": "You don't have access to any lab profiles. If you feel this is in error, please contact OSL Admin.",
                "default": False,  # Setting to False will ensure that the profile option is shown and not automatically started
                "kubespawner_override": {
                    "node_selector": {"opensciencelab.local/node-type": "no_profiles"},
                },
            }
        ]

    try:
        username: str = spawner.user.name
        auth_state: dict = await spawner.user.get_auth_state()
        portal_domain: str = await _get_portal_host(auth_state)

        user_data: dict = await _get_data_from_auth_api(username, portal_domain)
        # user_data Schema
        #
        # {
        #     'groups': [],
        #     'roles': ['user'],
        #     'name': 'username',
        #     'kind': 'user',
        #     'admin': False,
        #     'has_2fa': 1,
        #     'force_user_profile_update': False,
        #     'country_code': 'US',
        #     'lab_access': {
        #         'asfe-temp': {
        #             'lab_profiles': ['m6a.large'],
        #             'lab_country_status': 'unrestricted',
        #             'can_user_access_lab': False,
        #             'can_user_see_lab_card': False,
        #             'time_quota': None
        #         },
        #     },
        # }

        logging.warning(f">>>>>> Auth API data: {user_data}")

        lab_access_for_user: dict = user_data.get("lab_access", {}).get(
            LAB_SHORT_NAME, {}
        )
        can_user_access: bool = bool(
            lab_access_for_user.get("can_user_access_lab", False)
        )
        lab_profile_names_for_user: list = lab_access_for_user.get("lab_profiles", [])

        print(
            f"Lab profiles and group list for user '{username}': {lab_profile_names_for_user} with access status '{can_user_access}'"
        )

        if not can_user_access or len(lab_profile_names_for_user) == 0:
            return no_profiles()

        # Subset of profiles that the user can see
        lab_profiles = [
            lab_profile
            for lab_profile in LAB_PROFILES
            if lab_profile["name"] in lab_profile_names_for_user
        ]

        kubespawner_profile_list = []

        for lab_profile in lab_profiles:
            node_name_escaped = re.sub(
                r"[^A-Za-z0-9]", "00", lab_profile["node"].strip()
            )

            lab_hook_script = lab_profile.get("hook_script", None)
            if lab_hook_script:
                lifecycle_hook_cmd = (
                    f"bash /etc/user_server_includes/hooks/{lab_hook_script}"
                )
            else:
                lifecycle_hook_cmd = "echo No hook script ran."

            escaped_lab_profile_name = lab_profile["name"].replace(" ", "_")

            kubespawner_profile_dict = {
                "display_name": lab_profile["name"],
                "slug": urllib.parse.quote(lab_profile["name"]),  # type: ignore
                "description": lab_profile["description"],
                "default": lab_profile.get("default", None),
                "kubespawner_override": {
                    "extra_labels": {
                        "opensciencelab.local/node-type": f"user-{node_name_escaped}",
                        "opensciencelab.local/user-profile-name": escaped_lab_profile_name,
                        "sidecar.istio.io/inject": "false",
                        "opensciencelab.local/egress-profile": "none",
                    },
                    "node_selector": {
                        "opensciencelab.local/node-type": f"user-{node_name_escaped}"
                    },
                    "image": lab_profile["image_url"],
                    "lifecycle_hooks": {
                        "postStart": {
                            "exec": {"command": ["/bin/sh", "-c", lifecycle_hook_cmd]}
                        }
                    },
                    "args": [
                        "--YDocExtension.disable_rtc=True",
                        "--FileContentsManager.always_delete_dir=True",
                        "--FileContentsManager.delete_to_trash=False",
                    ],
                    "mem_limit": lab_profile.get("memory_limit", None),
                    "memory_guarantee": lab_profile.get("memory_guarantee", None),
                    "cpu_limit": lab_profile.get("cpu_limit", None),
                    "cpu_guarantee": lab_profile.get("cpu_guarantee", None),
                    "delete_pvc": lab_profile.get("delete_pvc", False),
                    "storage_capacity": lab_profile.get("storage_capacity", "10Gi"),
                    "environment": {
                        "JUPYTERHUB_SINGLEUSER_APP": "jupyter_server.serverapp.ServerApp",
                        "OPENSARLAB_PROFILE_NAME": lab_profile["name"],
                        "OPENSCIENCELAB_LAB_SHORT_NAME": LAB_SHORT_NAME,
                        "OPENSCIENCELAB_PORTAL_DOMAIN": portal_domain,
                    },
                    "default_url": lab_profile.get("default_url", "/lab"),
                },
            }
            kubespawner_profile_list.append(kubespawner_profile_dict)

        if kubespawner_profile_list == []:
            return no_profiles()

        # This clause for sudo should always be last
        if "sudo" in lab_profile_names_for_user:
            print("Adding sudo privs...")
            spawner.args.append("--allow-root")
            spawner.environment["GRANT_SUDO"] = "yes"
            # Sudo user group 599 is defined in the user image dockerfile:
            # RUN addgroup -gid 599 elevation && echo '%elevation ALL=(ALL) NOPASSWD: ALL' >> /etc/sudoers
            spawner.gid = 599
            spawner.allow_privilege_escalation = True

            # Users should know that sudo is enabled in lab profile before entering
            for ks_profile in kubespawner_profile_list:
                ks_profile["display_name"] += " (Sudo Enabled)"
        else:
            print("Sudo privs not given.")

        return kubespawner_profile_list

    except Exception as e:
        print("Something went wrong with the lab profiles list...")
        print(e, traceback.print_exc())
        return no_profiles()


# The variable "c" is a global variable representing the Config instance.
# This code will be appended to the end of the jupyterhub config.
# Linters like Flake8 often fail to recognize "magic" variables like "c".
# Therefore we apply "noqa: F821"
c.KubeSpawner.profile_list = lab_profile_list_hook  # noqa: F821
