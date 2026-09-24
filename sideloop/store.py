import os
import re
import shutil
import time

from . import ipa
from .config import APPS, DEVICES, read_env, write_env

APP_KEYS = ["BUNDLE_ID", "APP_NAME", "APP_VERSION", "IPA_SOURCE", "ADDED", "DEVICES"]
DEVICE_KEYS = ["DEVICE_NAME", "ADDED"]
UDID_RE = re.compile(r"^[0-9A-Fa-f-]{20,64}$")


def read_state(folder):
    def rd(name):
        try:
            return (folder / name).read_text().strip()
        except OSError:
            return ""
    lr = rd("last_run").split("\t")
    last_run = None
    if len(lr) >= 2 and lr[0].isdigit():
        last_run = {"t": int(lr[0]), "result": lr[1], "message": lr[2] if len(lr) > 2 else ""}
    return {"expiry": int(rd("expiry") or 0) or None, "last_run": last_run}


def device_ids():
    return sorted(f.stem for f in DEVICES.glob("*.env")) if DEVICES.is_dir() else []


def resolve_device(udid):
    return {u.lower(): u for u in device_ids()}.get(str(udid).lower())


def device_name(udid):
    return read_env(DEVICES / f"{udid}.env", DEVICE_KEYS)["DEVICE_NAME"] or udid


def add_device(udid, name):
    if not UDID_RE.match(udid):
        raise ValueError("that doesn't look like a device ID")
    new = udid not in device_ids()
    old = read_env(DEVICES / f"{udid}.env", DEVICE_KEYS)
    write_env(DEVICES / f"{udid}.env", {"DEVICE_NAME": name or udid, "ADDED": old["ADDED"] or int(time.time())})
    if new:
        for app_id in app_ids():
            devs = app_devices(app_id)
            if udid not in devs:
                set_app_devices(app_id, devs + [udid])


def remove_device(udid):
    (DEVICES / f"{udid}.env").unlink(missing_ok=True)
    for app_id in app_ids():
        set_app_devices(app_id, [u for u in app_meta(app_id)["DEVICES"].split() if u.lower() != udid.lower()])
        shutil.rmtree(APPS / app_id / "state" / udid, ignore_errors=True)


def app_ids():
    if not APPS.is_dir():
        return []
    return sorted(d.name for d in APPS.iterdir() if (d / "meta.env").exists() and (d / "app.ipa").exists())


def app_meta(app_id):
    return read_env(APPS / app_id / "meta.env", APP_KEYS)


def app_devices(app_id, meta=None):
    known = {u.lower(): u for u in device_ids()}
    return [known[u.lower()] for u in (meta or app_meta(app_id))["DEVICES"].split() if u.lower() in known]


def set_app_devices(app_id, udids):
    meta = app_meta(app_id)
    meta["DEVICES"] = " ".join(udids)
    write_env(APPS / app_id / "meta.env", meta)


def app_view(app_id):
    d, meta = APPS / app_id, app_meta(app_id)
    return {
        "id": app_id,
        "bundle_id": meta["BUNDLE_ID"],
        "name": meta["APP_NAME"] or app_id,
        "version": meta["APP_VERSION"],
        "info": ipa.cached(d),
        "targets": [{"udid": u, "name": device_name(u), "signature": read_state(d / "state" / u)}
                    for u in app_devices(app_id, meta)],
    }


def add_app(src, info, filename):
    app_id = re.sub(r"[^A-Za-z0-9._-]", "_", info["bundle_id"])[:120] or "app"
    d = APPS / app_id
    (d / "state").mkdir(parents=True, exist_ok=True)
    os.replace(src, d / "app.ipa")
    old = app_meta(app_id)
    write_env(d / "meta.env", {
        "BUNDLE_ID": info["bundle_id"],
        "APP_NAME": info.get("name", ""),
        "APP_VERSION": info.get("version", ""),
        "IPA_SOURCE": filename[:200],
        "ADDED": old["ADDED"] or int(time.time()),
        "DEVICES": old["DEVICES"] if old["ADDED"] else " ".join(device_ids()),
    })
    (d / "info.json").unlink(missing_ok=True)
    return app_id


def remove_app(app_id):
    shutil.rmtree(APPS / app_id)
