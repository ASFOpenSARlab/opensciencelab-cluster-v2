"""
Validate specific environment values and schemas.
"""

import re
import os
import tomllib  # type: ignore
import textwrap

#####
#
#  Validate the docker ref the safer way. Using a regex approach was way too slow.
#
#####

# Strict, flat rules matching OCI / Docker specifications
# No nested qualifiers (+ or *) inside other qualifiers to prevent backtracking
DOMAIN_COMPONENT = re.compile(r"^[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*$")
PATH_COMPONENT = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
TAG_REGEX = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9._-]{0,127}$")
DIGEST_REGEX = re.compile(r"^[A-Za-z][A-Za-z0-9+._-]*:[a-fA-F0-9]{32,}$")


def is_valid_docker_ref_safe(ref: str) -> bool:
    """Instantly validates a Docker reference using O(N) string splitting."""
    if not ref or len(ref) > 255:  # Docker refs have a 255 character limit
        return False

    # 1. Separate Digest (@)
    if "@" in ref:
        ref, digest = ref.rsplit("@", 1)
        if not DIGEST_REGEX.match(digest):
            return False

    # 2. Separate Tag (:)
    # Must look at rsplit but be careful not to mistake a port (e.g., localhost:5000) for a tag
    if ":" in ref:
        remainder, potential_tag = ref.rsplit(":", 1)
        # If the part after ':' contains a slash, it's a port inside a domain name, not a tag
        if "/" not in potential_tag:
            if not TAG_REGEX.match(potential_tag):
                return False
            ref = remainder

    # 3. Validate Name Components (Slashes)
    parts = ref.split("/")
    if not parts or any(not p for p in parts):
        return False

    # Check if the first part is a registry domain (contains '.' or ':' or 'localhost')
    first_part = parts[0]
    is_domain = "." in first_part or ":" in first_part or first_part == "localhost"

    if is_domain:
        # Validate domain port if present
        if ":" in first_part:
            domain, port = first_part.split(":", 1)
            if not port.isdigit() or not DOMAIN_COMPONENT.match(domain):
                return False
        elif not DOMAIN_COMPONENT.match(first_part):
            return False
        path_parts = parts[1:]
    else:
        path_parts = parts

    # Docker requires at least one path component if a domain is used
    if is_domain and not path_parts:
        return False

    # Validate remaining repository path elements
    return all(PATH_COMPONENT.match(p) for p in path_parts)


#####


def is_url_friendly(value: str) -> bool:
    # Matches strings containing only lowercase letters, numbers, and hyphens
    # Adjust regex pattern if you allow uppercase or underscores
    pattern = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
    return bool(re.match(pattern, value))


def is_valid_domain(domain: str) -> bool:
    # Overall domain length must not exceed 253 characters
    if not domain or len(domain) > 253:
        return False

    # Remove scheme
    domain = domain.replace("https://", "").replace("http://", "")

    # Must end with a valid Top-Level Domain (TLD) at least 2 characters long
    # Labels must be 1-63 characters, start/end with alphanumeric, and can contain hyphens
    pattern = r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$"

    return bool(re.match(pattern, domain))


def validate_ssm_cron_pure(cron_str):
    cron_str = cron_str.strip()

    # If wrapped in cron(...), extract the inner expression string
    wrapped_match = re.match(r"^cron\((.+)\)$", cron_str)
    if wrapped_match:
        cron_str = wrapped_match.group(1).strip()

    # Split inner expression into fields
    fields = cron_str.split()
    if len(fields) != 6:
        return False

    minute, hour, day_of_month, month, day_of_week, year = fields

    # 1. AWS Strict Day Wildcard Rule: One must be '?', the other cannot be '?'
    if (day_of_month == "?") == (day_of_week == "?"):
        return False

    # Helper regex factory for handling lists, ranges, and increments
    def validate_field(field, element_regex):
        # Checks if field matches single value, range (A-B), increments (*/X or A/X), or comma-separated lists
        pattern = rf"^({element_regex})(X({element_regex}))*$"
        # Replace allowed operators safely to evaluate individual components
        sanitized = field.replace(",", "X").replace("-", "X").replace("/", "X")
        return bool(re.match(pattern, sanitized))

    # 2. Minute Validation: 0-59 or *
    if minute != "*" and not validate_field(minute, r"([0-5]?\d)"):
        return False

    # 3. Hour Validation: 0-23 or *
    if hour != "*" and not validate_field(hour, r"(2[0-3]|[0-1]?\d)"):
        return False

    # 4. Day of Month Validation: 1-31, L, W, or ?
    # Allows numbers 1-31, optionally appended by L or W (e.g., 3W, L)
    dom_element = r"(([1-2]?\d|3[0-1])[LW]?|L|W|\?)"
    if day_of_month != "*" and not validate_field(day_of_month, dom_element):
        return False

    # 5. Month Validation: 1-12 or JAN-DEC or *
    months_abc = r"JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"
    month_element = rf"(1[0-2]|[1-9]|{months_abc})"
    if month != "*" and not validate_field(month, month_element):
        return False

    # 6. Day of Week Validation: 1-7, SUN-SAT, ?, L, or #
    # AWS Day of week accepts 1-7 (1=SUN) or 3-letter words, plus '#' or 'L' (e.g., 5#1, 2L, MON#3)
    dow_abc = r"SUN|MON|TUE|WED|THU|FRI|SAT"
    dow_element = rf"(([1-7]|{dow_abc})(#[1-5]|L)?|\?)"
    if day_of_week != "*" and not validate_field(day_of_week, dow_element):
        return False

    # 7. Year Validation: 1970-2199 or *
    year_element = r"(19[7-9]\d|2[0-1]\d\d)"
    if year != "*" and not validate_field(year, year_element):
        return False

    return True


def validate_profiles_and_nodes() -> None:
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

    print("""\n
##############
#
# Checking PROFILE_DEFINITIONS and NODE_DEFINITIONS...
#
##############\n""")

    profile_defs = os.getenv("PROFILE_DEFINITIONS")
    if not profile_defs:
        raise Exception("Profile definitions are not defined")

    print("PROFILE_DEFINITIONS...\n")
    print(textwrap.indent(profile_defs, "    "))
    print("\n")

    node_defs = os.getenv("NODE_DEFINITIONS")
    if not node_defs:
        raise Exception("Node definitions are not defined")

    print("NODE_DEFINITIONS...\n")
    print(textwrap.indent(node_defs, "    "))
    print("\n")

    profiles = tomllib.loads(profile_defs)
    nodes = tomllib.loads(node_defs)

    # Put config data into a format better for code interactions
    # {"name1": {"key1": "val1", "key2": "val2", ...}, {"node_name2"}: {}, ...} => [{"name": "name1", "key1": "val1", ...}, {"name": "name2", ...}, ...}
    profiles = [{"name": name} | body for name, body in profiles.items()]
    nodes = [{"name": name} | body for name, body in nodes.items()]

    assert "core" in [node["node_type"] for node in nodes], (
        "A node with node_type of 'core' is required."
    )

    # Cycle through nodes
    for node in nodes:
        print(f"Checking node: {node}")
        if node["node_type"] == "core":
            assert node["required"] is True, (
                "For the core node, 'required' must be True"
            )
            assert "name" in node, "The name of the node is required."
            assert node["name"] == "core", (
                "For the core node, the node's name must be 'core'"
            )
            assert any(item in node["instance"] for item in ["t3a.large"]), (
                "For the core node, the instance type must be 't3a.large'"
            )
            assert node["group_min_size"] == 1, (
                "For the core node, the group_min_size must be 1"
            )
            assert node["group_max_size"] == 1, (
                "For the core node, the group_max_size must be 1"
            )

        elif node["node_type"] == "user":
            assert "name" in node, "The name of the node is required."
            assert is_url_friendly(node["name"]), (
                "For an user node, the node name must be url friendly"
            )
            assert "group_min_size" in node, (
                "For an user node, the group_min_size is required"
            )
            assert 0 <= node["group_min_size"] < 1000, (
                "For an user node, the group_min_size must be between 0 and 1000"
            )
            assert "group_max_size" in node, (
                "For an user node, the group_max_size is required"
            )
            assert 0 < node["group_max_size"] <= 1000, (
                "For an user node, the group_max_size must be between 0 and 1000"
            )
            assert node["group_min_size"] <= node["group_max_size"], (
                "For an user node, the group_min_size must be smaller than group_max_size"
            )
            assert isinstance(node.get("root_volume_size", 20), int), (
                "Node 'root_volume_size' must be an integer. There is no size (e.g. 'GB') appended."
            )
            assert node.get("root_volume_size", 20) >= 20, (
                "Node 'root_volume_size' must be bigger than 20. In general, the size must be bigger than node's AMI's root volume snapshot."
            )

        else:
            assert node["node_type"] == "unknown", "node_type must be 'core' or 'user'"

    for profile in profiles:
        print(f"Checking user profile: {profile}")
        assert "node" in profile, "Profile node is required"
        assert profile["node"] in [node["name"] for node in nodes], (
            "Profile node must match an existing node name"
        )
        assert "image_url" in profile, "Profile image_url is required"
        assert is_valid_docker_ref_safe(profile["image_url"]), (
            "Profile image_url must be a valid docker ref"
        )
        assert isinstance(profile.get("default", False), bool), (
            "Profile 'default' (to set profile as default) must be boolean"
        )
        assert profile.get("hook_script", ".sh").endswith(".sh"), (
            "Profile 'hook_script' must end in '.sh'"
        )
        assert "storage_capacity" in profile, "Profile storage_capacity is required"
        assert profile.get("storage_capacity", "Gi").endswith(("Gi", "Ti", "T", "G")), (
            "Profile 'storage_capacity' must end in 'Gi', 'Ti', 'T', or 'G'"
        )
        assert profile.get("mem_limit", "G").endswith(("G", "M")), (
            "Profile 'mem_limit' must end in 'G' or 'M'"
        )
        assert profile.get("memory_guarantee", "G").endswith(("G", "M")), (
            "Profile 'memory_guarantee' must end in 'G' or 'M'"
        )
        assert isinstance(profile.get("cpu_limit", 1), (int, float)), (
            "Profile 'cpu_limit' must be a int or float between 0.5 and the max cpu cores available"
        )
        assert isinstance(profile.get("cpu_guarantee", 1), (int, float)), (
            "Profile 'cpu_guarantee' must be a int or float between 0.5 and the max cpu cores available"
        )
        assert isinstance(profile.get("delete_pvc", False), bool), (
            "Profile 'delete_pvc' (to delete the user's pvc on server shutdown) must be boolean"
        )
        assert profile.get("default_url", "/lab") in ["/lab", "/desktop"], (
            "Profile 'default_url' (to set the url where the user server starts) must be '/lab' or '/desktop'"
        )

    print("\n... All good!")


def validate_other_environment_variables() -> None:
    print("""\n
##############
#
# Checking other environment variables...
#
##############\n""")

    print("Checking PORTAL_DOMAINS ....")
    portal_domains = os.getenv("PORTAL_DOMAINS")
    if not portal_domains:
        raise Exception("PORTAL_DOMAINS is not defined")

    assert portal_domains.split(","), "PORTAL_DOMAINS must be a comma-seperated string"

    domains_list = portal_domains.split(",")

    for domain in domains_list:
        assert domain.startswith(("http://", "https://")), (
            "Domains within PORTAL_DOMAINS must start with 'http://' or 'https://'"
        )
        assert is_valid_domain(domain), (
            "Domains within PORTAL_DOMAINS must be in a valid format"
        )

    print("Checking JUPYTER_HUB_IMAGE_PATH ....")
    jupyter_hub_image_path = os.getenv("JUPYTER_HUB_IMAGE_PATH")
    if not jupyter_hub_image_path:
        raise Exception("Jupyterhub hub image path is not defined")

    print("Checking JUPYTER_HUB_IMAGE_TAG ....")
    jupyter_hub_image_tag = os.getenv("JUPYTER_HUB_IMAGE_TAG")
    if not jupyter_hub_image_tag:
        raise Exception("Jupyterhub hub image tag is not defined")

    print("Checking optional EXECWHACKER_CRON_IMAGE_PATH ....")
    execwhacker_cron_image_path = os.getenv("EXECWHACKER_CRON_IMAGE_PATH", None)

    print("Checking optional EXECWHACKER_CRON_IMAGE_TAG ....")
    execwhacker_cron_image_tag = os.getenv("EXECWHACKER_CRON_IMAGE_TAG", None)

    print("Checking optional IS_CRYPTNONO_ENABLED ....")
    is_cryptnono_enabled = (
        os.getenv("IS_CRYPTNONO_ENABLED", "true").strip().lower() == "true"
    )
    if is_cryptnono_enabled and (
        not execwhacker_cron_image_path or not execwhacker_cron_image_tag
    ):
        raise Exception(
            "You cannot run crytnono without defining EXECWHACKER_CRON_IMAGE_TAG or EXECWHACKER_CRON_IMAGE_PATH"
        )

    print("Checking ALLOWED_LAB_PROFILES ....")
    allowed_lab_profiles = [
        profile.strip() for profile in os.getenv("ALLOWED_LAB_PROFILES", "").split(",")
    ]
    if allowed_lab_profiles == [""]:
        raise Exception("ALLOWED_LAB_PROFILES are not defined")

    print("Checking ADMIN_USERS ....")
    admin_users = [
        username.strip() for username in os.getenv("ADMIN_USERS", "").split(",")
    ]
    if admin_users == [""]:
        raise Exception("ADMIN_USERS are not defined")

    print("Checking PORTAL_DOMAINS ....")
    portal_domains = os.getenv("PORTAL_DOMAINS", None)
    if not portal_domains:
        raise Exception("PORTAL_DOMAINS is not defined")

    print("Checking VOLUME_CRON_SCHEDULE ....")
    volume_cron_schedule = os.getenv("VOLUME_CRON_SCHEDULE", None)
    if not volume_cron_schedule:
        raise Exception("VOLUME_CRON_SCHEDULE is not defined")
    assert validate_ssm_cron_pure(volume_cron_schedule), (
        "VOLUME_CRON_SCHEDULE must be in valid AWS SSM cron format"
    )

    print("Checking SNAPSHOT_WARNING_DAYS ....")
    snapshot_warning_days = os.getenv("SNAPSHOT_WARNING_DAYS", None)
    if not snapshot_warning_days:
        raise Exception("SNAPSHOT_WARNING_DAYS is not defined")

    print("Checking LAB_SHORT_NAME ....")
    lab_short_name = os.getenv("LAB_SHORT_NAME", None)
    if not lab_short_name:
        raise Exception("LAB_SHORT_NAME is not defined")
    assert is_url_friendly(lab_short_name), "LAB_SHORT_NAME must be url-friendly"

    print("Checking UI_IAM_USER ....")
    ui_iam_user = os.getenv("UI_IAM_USER", None)
    if not ui_iam_user:
        raise Exception("UI_IAM_USER is not defined")

    print("Checking DAYS_TILL_VOLUME_DELETION ....")
    days_till_volume_deletion = os.getenv("DAYS_TILL_VOLUME_DELETION", None)
    if not days_till_volume_deletion:
        raise Exception("DAYS_TILL_VOLUME_DELETION is not defined")
    assert int(days_till_volume_deletion), (
        "DAYS_TILL_VOLUME_DELETION must be an integer value"
    )

    print("Checking DAYS_TILL_SNAPSHOT_DELETION ....")
    days_till_snapshot_deletion = os.getenv("DAYS_TILL_SNAPSHOT_DELETION", None)
    if not days_till_snapshot_deletion:
        raise Exception("DAYS_TILL_SNAPSHOT_DELETION is not defined")
    assert int(days_till_snapshot_deletion), (
        "DAYS_TILL_SNAPSHOT_DELETION must be an integer value"
    )

    print("Checking optional AZ_LETTER ....")
    # Make sure everything happens in a particular AZ.
    # This is normally 'a' but can be 'b' or 'c' if more than one cluster is deployed in an account and resources will be limited.
    az_letter = os.getenv("AZ_LETTER", "a")
    assert az_letter in ["a", "b", "c"], (
        "The availability zone letter AZ_LETTER must be 'a', 'b', or 'c'"
    )

    print("\n... All good!")


if __name__ == "__main__":
    validate_profiles_and_nodes()
    validate_other_environment_variables()
