# hadolint global ignore=DL3008,3013,3059  # Ignore pinned dependencies and connsecutive RUNs
FROM quay.io/jupyterhub/k8s-hub:4.3.2 AS release

# By default, the notebook base image is set to non-sudo user joyvan. This makes root-ful actions difficult.
USER root

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && \
    apt-get install --no-install-recommends -y \
    tzdata

# Configue timezone
RUN ln -fs /usr/share/zoneinfo/America/Alaska /etc/localtime &&\
    dpkg-reconfigure --frontend noninteractive tzdata

# Install system libs
RUN apt-get install --no-install-recommends -y \
    python3 \
    python3-pip \
    less \
    vim \
    patch && \
    rm -rf /var/lib/apt/lists/*

# Install python packages to make our lives easier
RUN python3 -m pip install --no-cache-dir --upgrade \
    awscli \
    pyyaml \
    boto3 \
    kubernetes==34.1.0 \
    kubernetes_asyncio \
    opensarlab-backend==1.0.4 \
    --

# Not sure what this does and may not be needed
#RUN cp -r /tmp/site-packages/* /usr/local/lib/python*/site-packages

USER jovyan
