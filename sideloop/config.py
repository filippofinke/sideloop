import os
import shlex
import subprocess
import threading
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG = DATA / "config.env"
STATE = DATA / "state"
APPS = DATA / "apps"
DEVICES = DATA / "devices"
UI_FILE = DATA / "ui.json"
PENDING_IPA = DATA / ".pending.ipa"

MUX = os.environ.get("MUX", "builtin")
PORT = int(os.environ.get("UI_PORT", "8080"))
ANISETTE = os.environ.get("ANISETTE_SERVER", "http://127.0.0.1:6969")
USB_MUX = "/var/run/usbmuxd"
LOCKDOWN_DIR = "/var/lib/lockdown"
LIFETIME_DAYS = 7

KEYS = ["APPLE_ID", "APPLE_PASSWORD", "RENEW_BEFORE_DAYS", "AUTO_CHECK"]
DEFAULTS = {"RENEW_BEFORE_DAYS": "2", "AUTO_CHECK": "1"}

if MUX == "builtin":
    os.environ["USBMUXD_SOCKET_ADDRESS"] = "127.0.0.1:27015"
os.environ["DATA_DIR"] = str(DATA)
os.environ["ANISETTE_SERVER"] = ANISETTE

_lock = threading.Lock()


def read_env(path, keys, defaults=None):
    out = {k: (defaults or {}).get(k, "") for k in keys}
    if path.exists():
        script = 'set -a; . "$1"; shift; for k; do printf "%s\\0%s\\0" "$k" "${!k-}"; done'
        r = subprocess.run(["bash", "-c", script, "_", str(path), *keys], capture_output=True)
        parts = r.stdout.split(b"\0")
        for k, v in zip(parts[0::2], parts[1::2]):
            if v:
                out[k.decode()] = v.decode("utf-8", "replace")
    return out


def write_env(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.writelines(f"{k}={shlex.quote(str(v))}\n" for k, v in values.items())
    os.replace(tmp, path)


def read():
    return read_env(CONFIG, KEYS, DEFAULTS)


def update(values):
    with _lock:
        cfg = read()
        cfg.update({k: str(v) for k, v in values.items() if k in KEYS})
        write_env(CONFIG, cfg)
        return cfg


def renew_before_days(cfg=None):
    return int((cfg or read())["RENEW_BEFORE_DAYS"] or 2)
