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

Kubernetes handles user storage internally via the [kubernetes objects](https://kubernetes.io/docs/concepts/storage/persistent-volumes/) Persistent Volume Claim (PVC) and Persistent Volume (PV). These map directly to AWS EBS volumes and snapshots. To ensure users don't lose their data, snapshots are taken often. On server startup, storage assigned to the user is checked accoring to four scenerios:

1. If the user has an existing PVC it is assumed that they have an existing EBS volume. If this is not true, admins need to manually force-delete the PVC and restart the server.
2. If the user doesn't have an existing PVC nor EBS volume, then the PVC, PV, and EBS volume are created.
3. If the user doesn't have an existing PVC but does have an EBS volume, a PVC and PV is created from the volume.
4. If the user doesn't have an existing PVC nor EBS volume, but does have a EBS snapshot, an EBS volume with associated PVC and PV will be created.

If more than one EBS volume is found, the most recent one will be used when creating a new PVC.

If more than one EBS snapshot is found, the most recent one will be used when restoring an EBS volume.

If the restoring EBS snapshot has a size bigger than the configured value, the restored volume size will be the same as the snapshot.

The default size of user `storage_capacity` is 10GBi if not provided in configuration. Values for user `storage_capacity` can be applied to individual lab profiles in [opensciencelab.toml](./cluster-cdk/cluster_cdk/opensciencelab.toml). Once the volume is created, this storage value can not be changed via updating the lab profile. EBS volumes cannot be shrunk but they may be expanded. If volumes could be shrunk, users would lose data. Therefore, bigger storage sizes should be assigned carefully to avoid costs. To expand the user volume size, use AWS cloudshell and edit `spec.resources.requests.storage` within the user's PVC.

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
export AWS_PROFILE=<AWS PROFILE>
export DEPLOY_PREFIX=<YOUR INITIALS>

export JUPYTER_HUB_DOCKER_TAG=<OPENSCIENCELAB JUPYTERHUB IMAGE TAG>
export UI_IAM_USER=<AWS CONSOLE USER ROLE>

export LAB_SHORT_NAME=<LAB SHORT NAME>
export ALLOWED_LAB_PROFILES=<LIST OF PROFILES>
export ADMIN_USERS=<JUPYTERHUB ADMIN USERNAME>
export PORTAL_DOMAINS=<PORTAL CALLBACK CLOUDFRONT DOMAIN(S)>
```

For example configurations, see the [GitHub Environments](https://github.com/ASFOpenSARlab/opensciencelab-cluster-v2/settings/environments)
in the cluster repository.

Once you've updated the values of the variables in your `.env`, load them into your
environment:

```bash
source .env
```

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
