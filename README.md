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

#### AWS Accounts

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

### Troubleshooting

### Deploying the Cluster


##### Ensure AWS credentials are present

The Makefile + Docker process will need to communicate with AWS. In actions, this is done through an
OIDC Provider in AWS and requires no authentication. Locally however, a profile must be present in
`~/.aws/credentials` and the `AWS_DEFAULT_PROFILE` env var needs to be set accordingly, **_OR_**
`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` must be set. Either solution works, and will get
automagically populated into the dockerized build/deploy environment.

###### `~/.aws` configuration

You will need a section in `~/.aws/credentials` like

```txt
[clusterv2]
aws_access_key_id = <YOUR KEY ID HERE>
aws_secret_access_key = <YOUR KEY VALUE HERE>
```

and a section in `~/.aws/config` like

```txt
[clusterv2]
region = us-west-2
output = json
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
```
AWS_PROFILE=<AWS PROFILE>
DEPLOY_PREFIX=<YOUR INITIALS>

JUPYTER_HUB_DOCKER_TAG=<OPENSCIENCELAB JUPYTERHUB IMAGE TAG>
UI_IAM_USER=<AWS CONSOLE USER ROLE>

LAB_SHORT_NAME=<LAB SHORT NAME>
ALLOWED_LAB_PROFILES=<LIST OF PROFILES>
ADMIN_USERS=<JUPYTERHUB ADMIN USERNAME>
PORTAL_DOMAINS=<PORTAL CALLBACK CLOUDFRONT DOMAIN(S)>
```

For example configurations, see the [GitHub Environments](https://github.com/ASFOpenSARlab/opensciencelab-cluster-v2/settings/environments)
in the cluster repository.

Once you've updated the values of the variables in your `.env`, load them into your
environment:

```bash
set -a && source .env && set +a
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
