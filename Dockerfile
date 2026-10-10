ARG ALTSERVER_TAG=ng-2026-09-13

# AltServer is built from source with the patches in altserver/. Stock AltServer removes every
# free provisioning profile on the device before installing, which makes iOS forget that the user
# trusted the developer, so each refresh brought back "Untrusted Developer". It also loads each file
# byte by byte and sends it as one packet, so large app binaries time out over Wi-Fi.
FROM ghcr.io/nyamisty/altserver_builder_alpine_aarch64 AS altserver-arm64
FROM ghcr.io/nyamisty/altserver_builder_alpine_amd64 AS altserver-amd64
FROM altserver-${TARGETARCH} AS altserver
ARG ALTSERVER_TAG
COPY altserver/*.patch /tmp/patches/
RUN git clone -q --recursive --depth 1 --shallow-submodules -b "$ALTSERVER_TAG" \
      https://github.com/jaakkopalvaila/AltServer-Linux /src \
 && cd /src && git apply /tmp/patches/*.patch \
 && mkdir build && cd build && make -f ../Makefile -j"$(nproc)" \
 && cp AltServer-* /AltServer

FROM debian:trixie-slim

ARG NETMUXD_TAG=v0.4.3

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libimobiledevice-utils usbmuxd openssl python3 curl ca-certificates coreutils tzdata iproute2 \
 && rm -rf /var/lib/apt/lists/*

RUN arch="$(uname -m)" \
 && case "$arch" in aarch64|arm64) arch=aarch64 ;; x86_64) ;; *) echo "unsupported arch $arch"; exit 1 ;; esac \
 && curl -fsSL "https://github.com/jkcoxson/netmuxd/releases/download/${NETMUXD_TAG}/netmuxd-${arch}-unknown-linux-gnu.tar.gz" \
      | tar xz -C /usr/local/bin \
 && chmod +x /usr/local/bin/netmuxd

COPY --from=altserver /AltServer /usr/local/bin/AltServer
COPY scripts/ /usr/local/bin/
COPY sideloop/ /opt/sideloop/

ENV DATA_DIR=/data \
    PYTHONPATH=/opt \
    PYTHONUNBUFFERED=1
WORKDIR /data
EXPOSE 8080
CMD ["python3", "-m", "sideloop"]
