import os
import subprocess
import time

from . import lockdown, store
from .config import USB_MUX

USB_TIMEOUT = 300
TRUST_TIMEOUT = 180


def _sh(*argv):
    env = {k: v for k, v in os.environ.items() if k != "USBMUXD_SOCKET_ADDRESS"}
    r = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
    return r.returncode, (r.stdout + r.stderr).strip()


def _wait_for_usb(job):
    deadline = time.time() + USB_TIMEOUT
    while not job.cancelled and time.time() < deadline:
        out = _sh("idevice_id", "-l")[1].split()
        if out:
            return out[0]
        time.sleep(2)
    raise RuntimeError("no device showed up on USB")


def _pair(job, udid):
    asked, deadline = False, time.time() + TRUST_TIMEOUT
    while True:
        rc, out = _sh("idevicepair", "-u", udid, "pair")
        if rc == 0 and "SUCCESS" in out:
            return
        if not asked:
            job.say("On the device: tap “Trust”, then enter the passcode…")
            asked = True
        if job.cancelled or time.time() > deadline:
            raise RuntimeError(f"pairing wasn't confirmed on the device ({out})")
        time.sleep(2)


def pair(job, muxers):
    job.say("Plug the device into this machine with a cable and unlock it.")
    udid = _wait_for_usb(job)
    job.say(f"Found {udid}.")
    _pair(job, udid)
    job.say("✓ Paired.")
    if not lockdown.enable_wifi(udid, USB_MUX):
        raise RuntimeError("the device refused to switch on Wi-Fi connections")
    job.say("✓ Wi-Fi connections switched on.")
    name = _sh("ideviceinfo", "-u", udid, "-k", "DeviceName")[1] or udid
    store.add_device(udid, name)
    muxers.restart("netmuxd")
    job.say(f"✓ Added {name}. Your apps will be installed on it too.")
    job.say("Unplug the cable. It shows up over Wi-Fi within a minute while it's unlocked.")
