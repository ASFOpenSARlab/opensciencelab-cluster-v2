"""
Validate specific environment values and schemas.
"""

import os
import tomllib  # type: ignore


##########
#
#  Validate profiles and nodes.
#
#  Make sure that node names in user profiles match what is defined in the nodes section.
#
#  NODES:
#   required = boolean  # Optional. true if required regardless if a node uses it. Defaults to false.
#   node_type = "core | user"
#   instance = ["t3a.large", "other_instance_size"]  # The first instance size in the instance list will be the first to be tried, followed by any others in that order.
#   group_desired_size = number
#   group_min_size = number  # Defaults to zero
#   group_max_size = number  # Must be greater than one
#   root_volume_size = number  # Size in GiB of root EC2 volume. Default is 20. This number must usually be 20 or greater.
#
#  PROFILES:
#   description = "Hello world"
#   image_url = "http://..."  # FQDN path to JupyterLab image that will be started"
#   node = ""  # Name of the node that the image will be starting on. Needs to match any defined below."
#   hook_script = ""  # The hook script to run on server startup. Defaults to None (no script ran).
#   storage_capacity = ""  # The size of the EBS volume used for user's home directory. Defaults to 10Gi. This cannot be shrunk - only expanded. If the config value is smaller than current storage values, it will be ignored.
#
##########

SUPPORTED_INSTANCE_TYPES = [""]

profile_defs = os.getenv("PROFILE_DEFINITIONS")
if not profile_defs:
    raise Exception("Profile definitions are not defined")

node_defs = os.getenv("NODE_DEFINITIONS")
if not node_defs:
    raise Exception("Node definitions are not defined")

profiles = tomllib.loads(profile_defs)
nodes = tomllib.loads(node_defs)

# Put config data into a format better for code interactions
# {"name1": {"key1": "val1", "key2": "val2", ...}, {"node_name2"}: {}, ...} => [{"name": "name1", "key1": "val1", ...}, {"name": "name2", ...}, ...}
profiles = [{"name": name} | body for name, body in profiles.items()]
nodes = [{"name": name} | body for name, body in nodes.items()]

"""
{'core': {'required': True, 'node_type': 'core', 'instance': ['t3a.large'], 'group_desired_size': 1, 'group_min_size': 1, 'group_max_size': 1}, 'sar': {'node_type': 'user', 'instance': ['m5a.large', 'm6a.large'], 'group_desired_size': 0, 'group_min_size': 0, 'group_max_size': 5}, 'm6a-large': {'node_type': 'user', 'instance': ['m6a.large'], 'group_desired_size': 0, 'group_min_size': 0, 'group_max_size': 5, 'root_volume_size': 21}, 'm6a-xlarge': {'node_type': 'user', 'instance': ['m6a.xlarge'], 'group_desired_size': 0, 'group_min_size': 0, 'group_max_size': 2}}
"""

assert "core" in [node["name"] for node in nodes]

# Cycle through nodes
for node in nodes:
    if node["name"] == "core":
        assert node["required"] == "true"
        assert node["node_type"] == "core"
        assert node["instance"] in ["t3a.large"]
        assert node["group_desired_size"] == 1
        assert node["group_min_size"] == 1
        assert node["group_max_size"] == 1

    else:
        assert node["node_type"] == "user"
        assert node["instance"] in SUPPORTED_INSTANCE_TYPES

        # Optional
        if "group_desired_size" in node:
            assert 0 <= node["group_desired_size"] <= 1000
        assert 0 <= node["group_min_size"] < 1000
        assert 0 < node["group_max_size"] <= 1000
        assert (
            node["group_min_size"]
            <= node["group_desired_size"]
            <= node["group_max_size"]
        )

for profile in profiles:
    assert profile["node"] in [node["name"] for node in nodes]
