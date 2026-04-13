FROM quay.io/jupyterhub/k8s-hub:4.3.2 AS release

# By default, the notebook base image is set to non-sudo user joyvan. This makes root-ful actions difficult.
USER root

# Configue timezone
ENV DEBIAN_FRONTEND=noninteractive
# Ignore apt pinning and deleting apt list
# hadolint ignore=DL3008,DL3009
RUN apt-get update && \
    apt-get install --no-install-recommends -y \
    tzdata && \
    ln -fs /usr/share/zoneinfo/America/Alaska /etc/localtime &&\
    dpkg-reconfigure --frontend noninteractive tzdata

# Install system libs
# Ignore apt pinning
# hadolint ignore=DL3008
RUN apt-get install --no-install-recommends -y \
    python3 \
    python3-pip \
    less \
    vim \
    patch && \
    rm -rf /var/lib/apt/lists/*

# Install python packages to make our lives easier
# Ignore pip pinning 
# hadolint ignore=DL3013
RUN python3 -m pip install --no-cache-dir --upgrade \
    awscli \
    pyyaml \
    boto3 \
    kubernetes==34.1.0 \
    kubernetes_asyncio \
    opensarlab-backend==1.0.4 \
    helloworld \
    --

USER jovyan
