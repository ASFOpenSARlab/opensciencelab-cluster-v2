define HELP

Makefile commands:

    help:                   List makefile commands

    lint:                   Run linting commands

    cdk-shell:              Enter CDK environment Docker Image

    manual-cdk-bootstrap:   Bootstrap an account for CDK. Especially for OIDC.

    test:                   Run PyTest tests

    run-volume-lambda:      Attempt to execute volume lambda locally

    synth-cluster:          Synth cluster CDK project

    deploy-cluster:         Deploy cluster CDK project

    synth-oidc:             Synth OIDC CDK project

    deploy-oidc:            Deploy OIDC CDK project

    aws-info:               Get AWS account info

    clean:                  Remove .build/ & cdk.out/

endef
export HELP

# work in BASH
SHELL:=/bin/bash

# Get terminal colors
_SUCCESS := "\033[32m%s\033[0m %s\n" # Green text for "printf"
_DANGER := "\033[31m%s\033[0m %s\n" # Red text for "printf"

# Respect pathing
export PWD=$(dir $(realpath $(firstword $(MAKEFILE_LIST))))
PROJECT_DIR := $(if $(CI_PROJECT_DIR),$(CI_PROJECT_DIR:/=),$(PWD:/=/))
BUILD_DEPS ?= /tmp/.build/lambda/python

# These env vars should be defined in either a local or github environment
IMAGE_NAME ?= ghcr.io/asfopensarlab/osl-utils:main
AWS_DEFAULT_PROFILE := $(AWS_DEFAULT_PROFILE)
AWS_REGION ?= us-west-2
IS_PROD ?= false

JUPYTER_HUB_DOCKER_TAG ?= test
UI_IAM_USER := $(UI_IAM_USER)

# Extra env var for running volume management lambda
CLUSTER_NAME ?= eks-cluster-$(DEPLOY_PREFIX)


.PHONY := all
all: help

.PHONY := help
help:
	@echo "$$HELP"

.PHONY := lint
lint: remove-cdk-out
	echo "Starting Docker Shell..."
	echo ""
	docker run \
	    $$ARCH_OVERRIDE \
		-v "$$(pwd):/code" \
		-it \
		--rm \
		--pull always \
		${IMAGE_NAME} \
		make all || \
		(  echo -e "" && echo  'If docker run fails with "no matching manifest", ' \
		  'try setting ARCH_OVERRIDE: `export ARCH_OVERRIDE=--platform linux/amd64`.' && \
		  echo -e "" && exit -1 ) && \
	echo "### All Linting Passed ###" || \
	echo "⚠️⚠️⚠️ Linting was not successful ⚠️⚠️⚠️"

### CDK Environment
.PHONY := cdk-shell
cdk-shell:
	export AWS_DEFAULT_ACCOUNT=`aws sts get-caller-identity --query 'Account' --output=text` && \
	export AWS_DEFAULT_REGION="${AWS_REGION}" && \
		if [ -z "$$AWS_DEFAULT_ACCOUNT" ]; then echo "⚠️  Can't infer AWS credentials! ⚠️"; fi && \
	mkdir -p /tmp/cdkawscli/cache && \
	docker run --rm -it \
		$$ARCH_OVERRIDE \
		-v ~/.aws/:/root/.aws/:ro \
		-v ${PROJECT_DIR}/:/code/ \
		-e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
		-e AWS_DEFAULT_PROFILE -e AWS_PROFILE \
		-e AWS_DEFAULT_REGION -e AWS_REGION \
		-e AWS_DEFAULT_ACCOUNT \
		-e DEPLOY_PREFIX \
		-e JUPYTER_HUB_DOCKER_TAG \
		-e UI_IAM_USER \
		-e ADMIN_USERS \
		-e PORTAL_DOMAINS \
		-e ALLOWED_LAB_PROFILES \
		-e LAB_SHORT_NAME \
		-e VOLUME_CRON_SCHEDULE \
		-e SNAPSHOT_WARNING_DAYS \
		-e DAYS_TILL_VOLUME_DELETION \
		-e DAYS_TILL_SNAPSHOT_DELETION \
		-e AWS_CLI_PATH \
		-e CLUSTER_NAME \
		-e SSO_SECRET_ARN \
		-w /code/ \
		--pull always \
		${IMAGE_NAME} || \
		(  echo -e "" && echo  'If docker run fails with "no matching manifest", ' \
		  'try setting ARCH_OVERRIDE: `export ARCH_OVERRIDE=--platform linux/amd64`.' && \
		  echo -e "" )

.PHONY := manual-cdk-bootstrap
manual-cdk-bootstrap:
	export AWS_DEFAULT_ACCOUNT=`aws sts get-caller-identity --query 'Account' --output=text` && \
	export AWS_DEFAULT_REGION="${AWS_REGION}" && \
	if [ -z "${AWS_DEFAULT_PROFILE}" ]; then echo "AWS_DEFAULT_PROFILE is not set"; fi && \
	if [ -z "$$AWS_DEFAULT_ACCOUNT" ]; then echo "⚠️  Can't infer AWS credentials from AWS_DEFAULT_ACCOUNT! ⚠️" && exit; fi && \
	echo "Make sure we bootstrap credentials manually once per AWS account" && \
	read -p "Are you sure? [y/N] " ans && ans=$${ans:-N} && \
	if [ $${ans} = y ] || [ $${ans} = Y ]; then \
		printf $(_SUCCESS) "Running: \`cdk bootstrap aws://$$AWS_DEFAULT_ACCOUNT/$$AWS_DEFAULT_REGION\`..." && \
		cdk bootstrap aws://$$AWS_DEFAULT_ACCOUNT/$$AWS_DEFAULT_REGION --public-access-block-configuration false ; \
	else \
		printf $(_DANGER) "Aborted" ; \
	fi

.PHONY := install-reqs
install-reqs:
	echo "Installing CDK Build Deps" && \
    mkdir -p /tmp/.build/ && \
    pip freeze > /tmp/.build/installed && \
    ( ( cat cluster-cdk/requirements.txt | \
    	grep -v "^#" | \
    	cut -d'=' -f1 | \
    	xargs -I{} grep -q {} /tmp/.build/installed && \
	  echo "All build modules exists" ) || \
	  ( echo "Installing cluster-cdk/requirements.txt" && \
		pip install -r cluster-cdk/requirements.txt ) )

.PHONY := bundle-deps
bundle-deps:
	echo "Checking if ${BUILD_DEPS} exists..." && \
	if [[ ! -d ${BUILD_DEPS} ]]; then \
		mkdir -p ${BUILD_DEPS} && \
		pip install \
			-r cluster-cdk/cluster_cdk/lambdas/requirements.txt \
			--platform manylinux2014_x86_64 \
			--python-version 3.13 \
			--only-binary=:all: \
			-t ${BUILD_DEPS} \
			--upgrade ; \
	else \
		echo "Skipping deps bundled in ${BUILD_DEPS}. Remove to rebuild."; \
	fi

.PHONY := run-volume-lambda
run-volume-lambda: bundle-deps
	export PYTHONPATH="${BUILD_DEPS}:$${PYTHONPATH}" && \
	export CLUSTER_NAME="${CLUSTER_NAME}" && \
	python3 cluster-cdk/cluster_cdk/lambdas/volume_management.py

.PHONY := test
test: remove-cdk-out install-reqs bundle-deps
	@echo "Running tests for Cluster (${DEPLOY_PREFIX})"

.PHONY := synth-cluster
synth-cluster: install-reqs bundle-deps
	@echo "Synthesizing ${DEPLOY_PREFIX}/cluster-cdk"
	cd ./cluster-cdk && cdk synth

.PHONY := deploy-cluster
deploy-cluster: install-reqs bundle-deps
	@echo "Deploying ${DEPLOY_PREFIX}/cluster-cdk"
	cd ./cluster-cdk && cdk --require-approval never deploy

.PHONY := destroy-cluster
destroy-cluster: install-reqs
	@echo "Destroying ${DEPLOY_PREFIX}/cluster-cdk"
	cd ./cluster-cdk && cdk destroy --force --all

.PHONY := remove-cdk-out
remove-cdk-out:
	find . -name "cdk.out" | xargs -n 1 rm -rf

.PHONY := clean
clean:
	rm -rf /tmp/.build/ && \
	rm -rf ./cluster-cdk/cdk.out/

.PHONY := synth-oidc
synth-oidc:
	@echo "Synthesizing ${DEPLOY_PREFIX}/oidc-cdk"
	cd ./oidc-cdk && cdk synth

.PHONY := deploy-oidc
deploy-oidc:
	@echo "Deploying ${DEPLOY_PREFIX}/oidc-cdk"
	cd ./oidc-cdk && cdk --require-approval never deploy

.PHONY := aws-info
aws-info:
	@echo -n "AWS User: "
	@aws sts get-caller-identity \
		--query "$${query:-Arn}" \
		--output text
	@echo "AWS Default Region: $$AWS_DEFAULT_REGION"
	@echo "AWS Default Profile: $$AWS_DEFAULT_PROFILE"
