from typing import List, Dict
import logging
import json
import traceback

from tornado.httpclient import AsyncHTTPClient

from opensarlab.auth import encryptedjwt

NODES = []
LAB_PROFILES = json.loads("$LAB_PROFILES")
LAB_SHORT_NAME = ""
PORTAL_DOMAIN = ""
PORTAL_DOMAINS = ""

class My401Exception(Exception):
    pass

class MyErrorException(Exception):
    pass

async def _get_portal_host(auth_state):
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

    return PORTAL_DOMAIN

async def _get_data_from_auth_api(username: str, portal_domain: str):
    try:
        body = json.dumps({ 'username': f"{username}" })

        response = await AsyncHTTPClient().fetch(
                f"{portal_domain}/portal/hub/auth",
                body = body,
                method="POST"
            )

        if not response.code == 200:
            logging.error(f"Auth response code is not 200. Code: {response.code}, {response['message']}")
            raise MyErrorException()

        response = json.loads(response.body)
        if 'ERROR' in response['message']:
            logging.error(f"{response['message']}")
            raise MyErrorException()

    except Exception as e:
        logging.error(f"Something went wrong with retrieving authentication. {e}")
        raise My401Exception()

    try:
        jwt_data = encryptedjwt.decrypt(response['data'])
    except Exception as e:
        self.log.error(f"Profiles.py JWT decryption went wrong: {e}")

    return jwt_data

async def lab_profile_list_hook(spawner: c.Spawner) -> List[Dict]:  # noqa: F821

    # If nothing has been assigned to the user, create a dummy noop option for the default.
    # This will attempt to find a "noop" node to spin up and obviously fail.
    # Otherwise, the default profile is to spin up a basic jupyterlab server on a randomly selected node.
    def return_noop():
        return [{
            'display_name': 'noop',
            'slug': 'noop',
            'description': "You don't have access to any lab profiles. If you feel this is in error, please contact OSL Admin.",
            'default': 'True',
            'kubespawner_override': {
                'node_selector': {
                    'opensciencelab.local/node-type': 'noop'
                },
            }
        }]

    try:
        username = spawner.user.name
        auth_state = await spawner.user.get_auth_state()
        portal_domain = await _get_portal_host(auth_state)
        user_data = await _get_data_from_auth_api(username, portal_domain)

        logging.warning(f">>>>>> Auth API data: {user_data}")
        """
        {
            'groups': [], 
            'roles': ['user'], 
            'name': 'username', 
            'kind': 'user', 
            'admin': False, 
            'has_2fa': 1, 
            'force_user_profile_update': False, 
            'country_code': 'US', 
            'lab_access': {
                'asfe-temp': {
                    'lab_profiles': ['m6a.large'], 
                    'lab_country_status': 'unrestricted', 
                    'can_user_access_lab': False, 
                    'can_user_see_lab_card': False, 
                    'time_quota': None
                }, 
            },
            'access': [  ## This will be deprecated
                {
                    'asfe-temp': {
                        'lab_profiles': ['m6a.large'], 
                        'lab_country_status': 'unrestricted', 
                        'can_user_access_lab': True, 
                        'can_user_see_lab_card': True, 
                        'time_quota': None
                    }
                }
            ]
        }
        """

        lab_access_for_user: dict = user_data.get('lab_access', {}).get(LAB_SHORT_NAME, {})
        can_user_access: bool = bool(lab_access_for_user.get('can_user_access_lab', False))
        lab_profiles_for_user: list = lab_access_for_user.get('lab_profiles', [])

        groups_for_user: list = user_data.get('groups', [])
        groups_list_for_user: list = groups_for_user + lab_profiles_for_user

        print(f"Lab profiles and group list for user '{spawner.user.name}': {groups_list_for_user} with access status '{can_user_access}'")

        if can_user_access == False or len(groups_list_for_user) == 0:
            return_noop()

        kubespawner_profile_list = []

        for lab_profile in LAB_PROFILES:
            {% set node_name_escaped = lab_profile.node_name | regex_replace ("[^A-Za-z0-9]","00") | trim -%}
            node_name_escaped = ""

            if lab_profile['name'] in groups_list_for_user:
                kubespawner_profile_dict = {
                    'display_name': lab_profile['name'],
                    'slug': '{{ lab_profile.name | urlencode }}',
                    'description': lab_profile['description'],
                    'default': lab_profile.get("default", None),
                    'kubespawner_override': {
                        'extra_labels': {
                            'opensciencelab.local/node-type': f'user-{ node_name_escaped }',
                            'opensciencelab.local/user-profile-name': lab_profile['name'].replace(" ", "_"),
                            'sidecar.istio.io/inject': 'false',
                            'opensciencelab.local/egress-profile': 'none',
                        },
                        'node_selector': {
                            'opensciencelab.local/node-type': f'user-{ node_name_escaped }'
                        },
                        'image': lab_profile['image_url'],
                        # {% if lab_profile.hook_script is defined and lab_profile.hook_script != 'None' -%}
                        # 'lifecycle_hooks': {
                        #     "postStart": {
                        #         "exec": {
                        #             "command": ["/bin/sh", "-c", "/etc/singleuser/hooks/{{ lab_profile.hook_script }}"]
                        #         }
                        #     }
                        # },
                        # {% else -%}
                        # 'lifecycle_hooks': {
                        #     "postStart": {
                        #         "exec": {
                        #             "command": ["/bin/sh", "-c", "echo No hook script ran."]
                        #         }
                        #     }
                        # },
                        # {% endif -%}
                        'args': [
                            #"--NotebookApp.jinja_template_vars={'PROFILE_NAME':'{{ lab_profile.name }}'}",
                            "--ServerApp.jinja_template_vars={'PROFILE_NAME': lab_profile['name'], 'LAB_SHORT_NAME':LAB_SHORT_NAME}",
                            "--YDocExtension.disable_rtc=True",
                            "--FileContentsManager.always_delete_dir=True",
                            "--FileContentsManager.delete_to_trash=False",
                        ],
                        'mem_limit': lab_profile.get('memory_limit', None),
                        'memory_guarantee': lab_profile.get('memory_guarantee', None),
                        'cpu_limit': lab_profile.get('cpu_limit', None),
                        'cpu_guarantee': lab_profile.get('cpu_guarantee', None),
                        'delete_pvc': lab_profile.get('delete_pvc', False),
                        'storage_capacity': lab_profile.get("storage_capacity", "10"),
                        'environment': {
                            'JUPYTERHUB_SINGLEUSER_APP': 'jupyter_server.serverapp.ServerApp',
                            'OPENSARLAB_PROFILE_NAME': lab_profile['name'], 
                            'OPENSCIENCELAB_LAB_SHORT_NAME': LAB_SHORT_NAME,
                            'OPENSCIENCELAB_PORTAL_DOMAIN': portal_domain,
                        },
                        'default_url': '/lab'
                    }
                }
                kubespawner_profile_list.append(kubespawner_profile_dict)

        if not kubespawner_profile_list:
            return_noop()

        # This clause for sudo should always be last
        if 'sudo' in groups_list_for_user:
            print("Adding sudo privs...")
            spawner.args.append('--allow-root')
            spawner.environment["GRANT_SUDO"] = "yes"
            spawner.gid = 599
            spawner.allow_privilege_escalation = True

            # Users should know that sudo is enabled in lab profile before entering
            for profile in kubespawner_profile_list:
                profile['display_name'] += " (Sudo Enabled)"
        else:
            print("Sudo privs not given.")

        return kubespawner_profile_list

    except Exception as e:            
        print("Something went wrong with the lab profiles list...")
        print(e, traceback.print_exc())
        return_noop()

c.KubeSpawner.profile_list = lab_profile_list_hook  # noqa: F821
