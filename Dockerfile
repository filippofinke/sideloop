FROM debian:trixie-slim

ARG ALTSERVER_TAG=ng-2026-09-13
ARG NETMUXD_TAG=v0.4.3

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libimobiledevice-utils usbmuxd openssl python3 curl ca-certificates coreutils tzdata \
 && rm -rf /var/lib/apt/lists/*

RUN arch="$(uname -m)" \
 && case "$arch" in aarch64|arm64) arch=aarch64 ;; x86_64) ;; *) echo "unsupported arch $arch"; exit 1 ;; esac \
 && curl -fsSL -o /usr/local/bin/AltServer \
      "https://github.com/jaakkopalvaila/AltServer-Linux/releases/download/${ALTSERVER_TAG}/AltServer-${arch}" \
 && curl -fsSL "https://github.com/jkcoxson/netmuxd/releases/download/${NETMUXD_TAG}/netmuxd-${arch}-unknown-linux-gnu.tar.gz" \
      | tar xz -C /usr/local/bin \
 && chmod +x /usr/local/bin/AltServer /usr/local/bin/netmuxd

COPY scripts/ /usr/local/bin/
COPY sideloop/ /opt/sideloop/

ENV DATA_DIR=/data \
    PYTHONPATH=/opt \
    PYTHONUNBUFFERED=1
WORKDIR /data
EXPOSE 8080
CMD ["python3", "-m", "sideloop"]
