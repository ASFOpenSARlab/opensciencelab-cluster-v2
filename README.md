# opensciencelab-cluster-v2

## Architecture

At a high level, the new cluster is a highly customized JupyterHub on EKS, deployed via
a CDK + Actions pipeline.

![Architecture Diagram](docs/OSL%20Cluster%20v2%20Arch%20Diagram.svg)

[Read more about the choices and behavior of the OpenScienceLab-Cluster-V2 architecture](ARCHITECTURE.md).

## Required On Stack Deletion

> [!IMPORTANT]
> The network load balancer is created via annotaions on a custom k8s service resource. > When the CDK stack is destroyed,
> the underlying load balancer and networking is not automatically deleted.

> [!WARNING]
> Before deleting the stack, you MUST open AWS CloudShell and manually run `kubectl -n jupyter delete svc proxy-public-loadbalancer`.

If this is not done, resource deletion will hang and the load balancer and networking will fail deletion.
Then manual cleanup will need to occur and the whole process will take about two hours.## Troubleshooting

## Pre-Deployment Information

### AWS Accounts

| Maturity | Environment | AWS Account      |
| -------- | ----------- | ---------------- |
| `dev`    | Non-prod    | 97**\*\*\*\***89 |
| `test`   | Non-Prod    | 97**\*\*\*\***89 |
| `prod`   | Prod        | 70**\*\*\*\***05 |

These accounts are for OpenSARLab and development. Other deployments for labs, classes, etc are not included in this table.

**All cluster AWS account are assumed to be federated or children of another management account.** This is required for the `UI_IAM_ROLE` to work correctly.

### Maturities

- Non-`main` branches with specified prefix/suffix (eg `ab/ticket.feature`) are considered a dev maturity.
- Merges into `main` branch will create/update the `test` maturity deployment.
- Prod-level deployments (OpenSARLab, Custom Deployments) are manually deployed to via the deploy Action `workflow_dispatch`. Prod maturities are usually tags.

### On User Volumes and Snapshots

The following assumes that all EBS volumes and snapshots are tagged with `kubernetes.io/cluster/{cluster_name}=owned`.

Kubernetes handles user storage internally via the [kubernetes objects](https://kubernetes.io/docs/concepts/storage/persistent-volumes/) Persistent Volume Claim (PVC) and Persistent Volume (PV). These map directly to AWS EBS volumes and snapshots. To ensure users don't lose their data, snapshots are taken often. On server startup, storage assigned to the user is checked accoring to four scenerios:

1. If the user has an existing PVC it is assumed that they have an linked EBS volume. If this is not true and a PVC exists without a linked EBS volume, the PVC will be deleted to ensure consistency. Then a new PVC and volune will be created.
2. If the user doesn't have an existing PVC, EBS volume, nor ENS snapshot, then a new PVC, PV, and EBS volume will be created.
3. If the user doesn't have an existing PVC but does have an EBS volume, a PVC and PV is created from the volume. This should be rare. This case is most likely when a volume is manually created (or restored). Any EBS snapshots with the same claim name will be ignored.
4. If the user doesn't have an existing PVC nor EBS volume, but does have a EBS snapshot, an EBS volume will be restored and the associated PVC and PV will be created.

WARNING: Never have more than one EBS volume with the same `kubernetes.io/created-for/pvc/name` value. This will throw a 500 error for users.

WARNING: If more than one EBS snapshot is found with the same `kubernetes.io/created-for/pvc/name` value, the most recent will be restored.

WARNING: If an EBS volume `kubernetes.io/created-for/pvc/name` tag is manually changed, then the script will treat it like it doesn't exist. Since the existing PVC is referencing an apparently non-existing volume, the PVC will be deleted and the real volume will also be deleted. Therefore, NEVER modify the `kubernetes.io/created-for/pvc/name` EBS volume tag. It is safer to create a snapshot and restore a volume from that.

If the restoring EBS snapshot has a size bigger than the configured value, the restored volume size will be the same as the snapshot.

The size of the requested storage is `storage_capacity` as found in individual lab profiles within the `PROFILE_DEFINITIONS` environment variable. The default size of user `storage_capacity` is 10GBi if not provided in configuration. Once the volume is created, this storage value can not be changed via updating the lab profile. EBS volumes cannot be shrunk but they may be expanded. Therefore, bigger storage sizes should be assigned carefully to avoid costs.

There are two ways to expand the size of an existing volume:

1. Assign an user a lab profile with a bigger `storage_capacity`. If the `storage_capacity` of a profile is larger than the current volume size, the volume will expand to that size.
2. Use AWS cloudshell and edit `spec.resources.requests.storage` within the user's PVC. If the `storage_capacity` of a profile is larger than the current volume size, the volume will expand to that size.

Specific environment variables (can be set in the GitHub Environment) include

- `DAYS_TILL_VOLUME_DELETION`: The number of days after server stop when the EBS volume will be deleted. The default (if not set) is 3600.  
- `DAYS_TILL_SNAPSHOT_DELETION`: The number of days after server stop when the EBS snapshot will be deleted. The default (if not set) is 3600.

Various EBS tags are created on server start and stop. Some relevant ones are

- `server-start-tag`: The datetime the user's server started.
- `server-stop-tag`: The datetime the user's server stopped.
- `volume-delete-tag`: The datetime the EBS volume should be deleted. Calculated on server stop.
- `snapshot-delete-time`: The datetime the EBS snapshot should be deleted. Calculated on server stop.

## Building and Deploying the Cluster (GitHub Actions)

In actions, this is done through an OIDC Provider in AWS and requires no local authentication.

### Setup OIDC Provider within AWS (as needed)

One Provider is required per account and per region. If previously set up, this step can be disregarded.

Search CloudFormation for "OIDC". If not present, then create OIDC connection as follows:

1. Check /oidc-cdk/github_repos.conf to see if the GitHub repo containing cluster v2 code is present

2. Set AWS_DEFAULT_PROFILE. This is critical!

    `export AWS_DEFAULT_PROFILE=geos`

3. Within the root of the code,

    `make cdk-shell`

4. Check credentials and account

    `make aws-info`

5. Bootstrap CDK into account/region to we can use native CDK deploy features

    `make manual-cdk-bootstrap`

6. Add OIDC connection to account/region for GitHub use

    `make deploy-oidc`

CDKToolkit cloudformation template should be installed in account

### Setup GitHub Environment

Go to Settings > Secrets and variables > Actions.

There are three levels of precedence: Organization, Repository, and Environment.
Defaults for production OpenSARLab are held in Repository. Overrides are usually in Environment.

To add an environment, go to Settings > Environments, click _New Environment_, name the enviroment with the LAB_SHORT_NAME value,
and click Configure Environment.
The LAB_SHORT_NAME should be short and contain only number, letters, and hypens (i.e. url friendly).

Within the environment itself, in the _Environment secrets_ section, add AWS_ACCOUNT_NUMBER.

In the _Environment variables_ section, add any of the following overrides as needed.

```bash
# Infrastructure Configuration
JUPYTER_HUB_IMAGE_PATH="ghcr.io/asfopensarlab/opensciencelab-jupyterhub"  # Needs to exist for opensciencelab-jupyterhub image
JUPYTER_HUB_IMAGE_TAG="test"            # Needs to exist for opensciencelab-jupyterhub image
EXECWHACKER_CRON_IMAGE_PATH="ghcr.io/asfopensarlab/opensciencelab-update-execwhacker"  # Needs to exist for opensciencelab-update-execwhacker image
EXECWHACKER_CRON_IMAGE_TAG="test"       # Needs to exist for opensciencelab-update-execwhacker image
IS_CRYPTNONO_ENABLED="true"             # Is cryptnono deployed within the cluster?
UI_IAM_ROLE="AWSReservedSSO_Project.."  # IAM Role used by admins in the AWS console
ADMIN_USERS="nobody"                    # Comma seperated list of users to embed into JH

# PORTAL_DOMAINS is comma seperated portal urls with the first being the primary
# No commas allowed in names.
# The scheme (https://, http://) should not be included in the urls. Internally, only `https://` is used.
PORTAL_DOMAINS="<CLOUDFRONT-URL>, <CLOUDFRONT-URL>"

PROFILE_DEFINITIONS="""
  [ "GEOS 631" ]
  description = "JupyterLab 5 - RAM Guarantee: 5G. RAM limit: 8G. CPU limit: 2. Storage: 10G."
  image_url = "ghcr.io/asfopensarlab/geos631:test"
  node = "m6a-large"
  hook_script = "geos631.sh"
  memory_guarantee = "5G"
  storage_capacity = "10Gi"
"""

NODE_DEFINITIONS="""
  [ core ]
  required = true
  node_type = "core"
  instance = ["t3a.large"]
  group_min_size = 1
  group_max_size = 1

  [ m6a-large ]
  node_type = "user"
  instance = ["m6a.large"]
  group_min_size = 0
  group_max_size = 50
"""

VOLUME_CRON_SCHEDULE="0 * * * ? *"      # Schedule to run the volume management lambda
SNAPSHOT_WARNING_DAYS="5,3,1"           # Number of days before delete to warn for old snapshots
SNAPSHOT_GRACEPERIOD_DAYS=1           # Number of days to retain snapshot past deletion time

# Volume and snapshot lifecycle times
DAYS_TILL_VOLUME_DELETION=2             # Number of days after server stop when the user's volume will be deleted
DAYS_TILL_SNAPSHOT_DELETION=7           # Number of days after server stop when the user's snapshot will be deleted
```

### Validate Environment

To validate the environment before you deploy, run the [Validation GitHub Action](https://github.com/ASFOpenSARlab/opensciencelab-cluster-v2/actions/workflows/deploy-cluster-validate-env.yaml).

### Build with GitHub Actions

Run [Build GitHub Action](https://github.com/ASFOpenSARlab/opensciencelab-cluster-v2/actions/workflows/deploy-cluster-cdk-app.yaml)

You will need to select from the workflow dispatch the environment and the code to be deployed. Optionally, you can run the test suite.
Code merged into `main` will be automatically built on cluster `test`.

### After deploying

Go to [post build](#post-build).

## Building and Deploying the Cluster (Locally)

To increase development velocity, it might be easier and faster to locally push changes to AWS.
When making changes to non-dev maturities, use GitHub Actions to avoid environment corruption.

### Ensure AWS credentials are present on your computer

The Makefile + Docker process will need to communicate with AWS. There are two options to set AWS permssions:

Profile must be present in `~/.aws/credentials` and the `AWS_DEFAULT_PROFILE` env var needs to be set accordingly,
**_OR_** `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` must be set.

#### `~/.aws` configuration

You will need a section in `~/.aws/credentials` like

```txt
[clusterv2]
aws_access_key_id = <YOUR KEY ID HERE>
aws_secret_access_key = <YOUR KEY VALUE HERE>
```

You can generate AWS Access Keys from the IAM console:
[AWS docs](https://docs.aws.amazon.com/IAM/latest/UserGuide/access-key-self-managed.html#Using_CreateAccessKey)

### Update local environment variables

You can deploy a new stack without conflicting with any others.

First, create your environment. You can create a file for local development or add environment variables to GitHub.

For local developement create a file from the example:

```bash
cp dev.env.example .env
nano .env
```

`.env`:

```bash
# Required make variables for local development
export AWS_DEFAULT_PROFILE=me                  # The profile configured to access AWS Account
export LAB_SHORT_NAME="dd"                     # Short deployment prefix value

# Optional. For running storage management lambda outside AWS
export AWS_CLI_PATH=/usr/local/bin/aws         # Path to your installation of awscli
export SSO_SECRET_ARN=arn:aws:.....            # Arn location of cluster SSO secret

# Infrastructure Configuration
export JUPYTER_HUB_IMAGE_PATH="ghcr.io/asfopensarlab/opensciencelab-jupyterhub"  # Needs to exist for opensciencelab-jupyterhub image
export JUPYTER_HUB_IMAGE_TAG="test"            # Needs to exist for opensciencelab-jupyterhub image
export EXECWHACKER_CRON_IMAGE_PATH="ghcr.io/asfopensarlab/opensciencelab-update-execwhacker"  # Needs to exist for opensciencelab-update-execwhacker image
export EXECWHACKER_CRON_IMAGE_TAG="test"       # Needs to exist for opensciencelab-update-execwhacker image
export IS_CRYPTNONO_ENABLED="true"             # Is cryptnono deployed within the cluster?
export UI_IAM_ROLE="AWSReservedSSO_Project.."  # IAM Role used by admins in the AWS console
export ADMIN_USERS="nobody"                    # Comma seperated list of users to embed into JH
export PORTAL_DOMAINS="<CLOUDFRONT-URL>"       # Comma seperated list of approved portal domains

export VOLUME_CRON_SCHEDULE="0 * * * ? *"      # Schedule to run the volume management lambda
export SNAPSHOT_WARNING_DAYS="5,3,1"           # Number of days before delete to warn for old snapshots
export SNAPSHOT_GRACEPERIOD_DAYS=1           # Number of days to retain snapshot past deletion time

# Volume and snapshot lifecycle times
export DAYS_TILL_VOLUME_DELETION=2             # Number of days after server stop when the user's volume will be deleted
export DAYS_TILL_SNAPSHOT_DELETION=7           # Number of days after server stop when the user's snapshot will be deleted
```

For example configuration values, see the [GitHub Environments](https://github.com/ASFOpenSARlab/opensciencelab-cluster-v2/settings/environments)
in the cluster repository.

Once you've updated the values of the variables in your `.env`, load them into your
environment:

```bash
source .env
```

> [!CAUTION]
> This will override any other local environments.

### Pre-Deploy off `main` branch

Initial stack deployments take a long time. If the initial stack deploy fails, it takes
a very long time to delete and retry. It can be helpful to deploy the main stack prior
to beginning development to create a stable cluster before feature development.

### Start CDK Shell

From the root of the cloned repo, start the container:

```shell
$ make cdk-shell
[ root@a7a585db4d88:/cdk ]#
```

Run `make aws-info` and check the AWS user and account numbers:

```shell
[ root@a7a585db4d88:/cdk ]# make aws-info
```

Run `make synth-cluster` to test your environment:

```shell
[ root@a7a585db4d88:/cdk ]# cd /code
[ root@a7a585db4d88:/cdk ]# make synth-cluster
```

If you see CloudFormation after a few minutes, you're ready to deploy!

### Deploy via CDK

```shell
[ root@a7a585db4d88:/cdk ]# make deploy-cluster
```

### After deploying via CDK

Go to [post build](#post-build).

### Linting

Before committing changes, the code can be easily linted by utilizing the `lint` target of the Makefile. This will call the same linting routines used by the GitHub Actions.

## Post Build

There are a few steps after cluster build that need to be done to complete the full setup.

### Cluster Configuration

- From the initial build output, record the Load Balancer URL for later use.

- On initial build, the SNS Topic Subscription confirmation will be sent to the configured email. The given hyperlink will need to be clicked.
If the email doesn't show up in the inbox, check the spam folder. If it still doesn't show up, perhaps an internal firewall is blocking the email.
This possible blockage would also affect other OSL services and will need to be fixed.

### [OpenScienceLab SSO Portal](https://github.com/ASFOpenSARlab/opensciencelab-portal-v2) Integration

- Add Load Balancer URL to Portal [profile](https://github.com/ASFOpenSARlab/opensciencelab-portal-v2/blob/main/portal-cdk/lambda_main/util/labs/__init__.py).

- Update Portal SSO Token in cluster Secrets Manager. SSO Token is from Portal.

- Restart Hub pod (for SSO token changes to take effect).

   1. With the child AWS account, go to the EKS Console.
   2. Select the $LAB_SHORT_NAME cluster.
   3. Click on the Connect button in the upper right. This will take you to CloudShell with the proper kubectl credentials.
   4. Within CloudShell, run the command `kubectl -n jupyter delete pod -l component=hub`

## Troubleshooting

### My Cluster is inaccessible for some reason

Did you

- Change your SSO secret?
- Respawn the hub pod after changing SSO secret, or any other variables?
- Double check your portal lab card has the correct cluster deployment url?
