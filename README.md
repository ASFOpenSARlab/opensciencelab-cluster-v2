# opensciencelab-cluster-v2

## On Stack Deletion

The network load balancer is created via annotaions on a custom k8s service resource. When the CDK stack is destroyed,
the underlying load balancer and networking is not automatically deleted.

> [!WARNNG]
> Before deleting the stack, you MUST open AWS CloudShell and manually run `kubectl -n jupyter delete svc proxy-public-loadbalancer`.

If this is not done, resource deletion will hang and the load balancer and networking will fail deletion.
Then manual cleanup will need to occur and the whole process will take about two hours.

## Architecture

At a high level, the new cluster is a highly customized JupyterHub on EKS, deployed via
a CDK + Actions pipeline.

![Architecture Diagram](docs/OSL%20Cluster%20v2%20Arch%20Diagram.svg)

## Deployments

### AWS Accounts

| Maturity | Environment | AWS Account      |
| -------- | ----------- | ---------------- |
| `dev`    | Non-prod    | 97**\*\*\*\***89 |
| `test`   | Non-Prod    | 97**\*\*\*\***89 |
| `prod`   | Prod        | 70**\*\*\*\***05 |

### Maturities

- Non-`main` branches with specified prefix/suffix (eg `ab/ticket.feature`) will deploy a matched
  prefix (ie `ab`) dev maturity ( and `dev` GitHub environment!) deployment.
- Merges into `main` branch will create/update the `test` maturity deployment.
- Prod-level deployments (OpenSARLab, Custom Deployments) are manually deployed to via
  the deploy Action `workflow_dispatch`.

### Creation of User Volumes and Snapshots

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

The size of the requested storage is `storage_capacity` as found in individual lab profiles within [opensciencelab.toml](./cluster-cdk/cluster_cdk/opensciencelab.toml). The default size of user `storage_capacity` is 10GBi if not provided in configuration. Once the volume is created, this storage value can not be changed via updating the lab profile. EBS volumes cannot be shrunk but they may be expanded. Therefore, bigger storage sizes should be assigned carefully to avoid costs.

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

### Troubleshooting

### Deploying the Cluster

#### Ensure AWS credentials are present

The Makefile + Docker process will need to communicate with AWS. In actions, this is done through an
OIDC Provider in AWS and requires no authentication. Locally however, a profile must be present in
`~/.aws/credentials` and the `AWS_DEFAULT_PROFILE` env var needs to be set accordingly, **_OR_**
`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` must be set. Either solution works, and will get
automagically populated into the dockerized build/deploy environment.

##### `~/.aws` configuration

You will need a section in `~/.aws/credentials` like

```txt
[clusterv2]
aws_access_key_id = <YOUR KEY ID HERE>
aws_secret_access_key = <YOUR KEY VALUE HERE>
```

You can generate AWS Access Keys from the IAM console:
[AWS docs](https://docs.aws.amazon.com/IAM/latest/UserGuide/access-key-self-managed.html#Using_CreateAccessKey)

##### Updating environment variables

You can deploy a new stack without conflicting with any others.

First, create your environments file from the example:

```bash
cp dev.env.example .env
nano .env
```

`.env`:

```bash
export AWS_PROFILE="profile_name"            # The profile configured to access AWS Account
export DEPLOY_PREFIX="eg"                    # Short deployment prefix value, probably initials

export JUPYTER_HUB_DOCKER_TAG="test"         # needs to exist for opensciencelab-jupyterhub image
export UI_IAM_USER="AWSReservedSSO_Project"  # IAM Role used by admins in the AWS console

export LAB_SHORT_NAME="eg"                   # Probably a duplicate of DEPLOY_PREFIX, not used for actions
export ALLOWED_LAB_PROFILES="sar"            # Comma seperated list of profiles to enable
export ADMIN_USERS="nobody, joe"             # Comma seperated list of users to embed into JH       
export PORTAL_DOMAINS="https://...."         # Comma seperated list of approved portal domains

export VOLUME_CRON_SCHEDULE="0 * * * ? *"    # Schedule to run the volume management lambda
export SNAPSHOT_WARNING_DAYS="5,3,1"         # Number of days before delete to warn for old snapshots

export DAYS_TILL_VOLUME_DELETION=2           # Number of days after server stop when the user's volume will be deleted
export DAYS_TILL_SNAPSHOT_DELETION=7         # Number of days after server stop when the user's snapshot will be deleted

export AWS_CLI_PATH=/usr/local/bin/aws       # Path to your installation of awscli
export SSO_SECRET_ARN=arn:aws:.....          # Arn location of cluster SSO secret
```

For example configurations, see the [GitHub Environments](https://github.com/ASFOpenSARlab/opensciencelab-cluster-v2/settings/environments)
in the cluster repository.

Once you've updated the values of the variables in your `.env`, load them into your
environment:

```bash
source .env
```

##### Pre-Deploy

Initial stack deployments take a long time. If the initial stack deploy fails, it takes
a very long time to delete and retry. It can be helpful to deploy the main stack prior
to beginning development to create a stable cluster before feature development.

##### Start CDK Shell

From the root of the cloned repo, start the container:

```shell
$ make cdk-shell
[ root@a7a585db4d88:/cdk ]#
```

Run `make synth-cluster` to test your environment:

```shell
[ root@a7a585db4d88:/cdk ]# cd /code
[ root@a7a585db4d88:/cdk ]# make synth-cluster
```

If you see CloudFormation after a few minutes, you're ready to deploy!

##### Deploy via CDK

```shell
[ root@a7a585db4d88:/cdk ]# make deploy-cluster
```

##### Linting

Before committing changes, the code can be easily linted by utilizing the `lint` target of the Makefile. This will call the same linting routines used by the GitHub actions.
