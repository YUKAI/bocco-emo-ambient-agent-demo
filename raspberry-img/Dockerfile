FROM debian:bookworm AS base

RUN apt-get update && apt-get install --no-install-recommends -y \
    build-essential \
    curl \
    git \
    ca-certificates \
    sudo \
    gpg \
    gpg-agent \
    dosfstools \
    kpartx \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://archive.raspberrypi.com/debian/raspberrypi.gpg.key \
    | gpg --dearmor > /usr/share/keyrings/raspberrypi-archive-keyring.gpg

ARG RPIIG_VERSION=v2.1.0
RUN git clone --branch ${RPIIG_VERSION} --depth 1 https://github.com/raspberrypi/rpi-image-gen.git

ARG TARGETARCH
RUN echo "Building for architecture: ${TARGETARCH}"
RUN /bin/bash -c '\
    case "${TARGETARCH}" in \
    arm64) echo "Building for arm64" && \
        apt-get update && \
        rpi-image-gen/install_deps.sh ;; \
    amd64) echo "Building for amd64 (QEMU emulation)" && \
        sed -i "s|\"${binfmt_misc_required}\" == \"1\"|! -z \"\"|g" rpi-image-gen/scripts/dependencies_check && \
        if cat /proc/filesystems | grep -q binfmt_misc; then \
            echo "binfmt_misc is supported" ; \
        else \
            echo "binfmt_misc is not supported. Install binfmt-support on your host machine" ; \
            exit 1 ; \
        fi && \
        apt-get update && \
        apt-get install --no-install-recommends -y \
        qemu-user-static dirmngr slirp4netns quilt parted \
        debootstrap zerofree libcap2-bin libarchive-tools \
        xxd file kmod bc pigz arch-test && \
        rpi-image-gen/install_deps.sh ;; \
    *) echo "Unsupported architecture: ${TARGETARCH}" && exit 1 ;; \
    esac'

ENV USER=imagegen
RUN useradd -u 4000 -ms /bin/bash "${USER}" \
    && echo "${USER}:${USER}" | chpasswd \
    && adduser ${USER} sudo
USER ${USER}
WORKDIR /home/${USER}

RUN cp -r /rpi-image-gen ~/
