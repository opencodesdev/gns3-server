# Dockerfile for GNS3 server development

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# Set the locale
ENV LANG=en_US.UTF-8
ENV LANGUAGE=en_US:en
ENV LC_ALL=en_US.UTF-8

# this environment is externally managed
ENV PIP_BREAK_SYSTEM_PACKAGES=1

RUN apt-get update && apt-get install -y software-properties-common
RUN add-apt-repository ppa:gns3/ppa
RUN apt-get update && apt-get install -y \
    locales \
    python3-pip \
    python3-dev \
    qemu-system-x86 \
    qemu-kvm \
    libvirt-daemon-system libvirt-clients \
    x11vnc \
    git

RUN locale-gen en_US.UTF-8

# Install uninstall to install dependencies
RUN apt-get install -y vpcs ubridge

ADD . /server
WORKDIR /server

RUN pip3 install --no-cache-dir -r /server/dev-requirements.txt

EXPOSE 3080

CMD [ "python3", "-m", "gns3server", "--port", "3080" ]