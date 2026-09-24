import os
import signal
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from . import store
from .config import ANISETTE, LOCKDOWN_DIR, USB_MUX
from .log import log

SCAN_INTERVAL = 60


class Muxers:
    NAMES = ("usbmuxd", "netmuxd")
    CMDS = {
        "usbmuxd": ["usbmuxd", "-f"],
        "netmuxd": ["netmuxd", "--host", "127.0.0.1", "-p", "27015", "--disable-unix",
                    "--upstream-usbmuxd", USB_MUX, "--plist-storage", LOCKDOWN_DIR],
    }

    def __init__(self):
        self.procs = {}
        self.lock = threading.Lock()

    def start(self):
        Path(LOCKDOWN_DIR).mkdir(parents=True, exist_ok=True)
        for name in self.NAMES:
            self._spawn(name)
            time.sleep(1)
        threading.Thread(target=self._supervise, daemon=True).start()

    def _spawn(self, name):
        with self.lock:
            log("starting", name)
            self.procs[name] = subprocess.Popen(self.CMDS[name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def restart(self, name):
        with self.lock:
            p = self.procs.get(name)
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        self._spawn(name)

    def _supervise(self):
        while True:
            time.sleep(5)
            for name in self.NAMES:
                with self.lock:
                    dead = self.procs[name].poll() is not None
                if dead:
                    log(name, "exited, restarting")
                    time.sleep(2)
                    self._spawn(name)


def run_group(argv, timeout):
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        return p.communicate(timeout=timeout)[0]
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        log(f"{argv[0]}: killed after {timeout}s")
        return ""


def _mux_reachable():
    host, port = os.environ.get("USBMUXD_SOCKET_ADDRESS", "127.0.0.1:27015").rsplit(":", 1)
    try:
        socket.create_connection((host, int(port)), timeout=2).close()
        return True
    except OSError:
        return False


def _anisette_up():
    try:
        urllib.request.urlopen(ANISETTE, timeout=3).read(64)
        return True
    except Exception:
        return False


def _probe():
    devices = []
    for line in run_group(["probe.sh"], 60).splitlines():
        p = line.split("\t")
        if len(p) == 6:
            devices.append({"udid": p[0], "conn": p[1], "reachable": p[2] == "yes",
                            "name": p[3], "model": p[4], "ios": p[5]})
    return devices


class Health:
    def __init__(self):
        self.lock = threading.Lock()
        self.devices = []
        self.here = set()
        self.anisette = False
        self.muxer = False
        self.scanning = False
        self.on_arrival = lambda udid: None

    def scan(self):
        with self.lock:
            if self.scanning:
                return
            self.scanning = True
        try:
            muxer, anisette = _mux_reachable(), _anisette_up()
            devices = _probe() if muxer else []
            known = {u.lower(): u for u in store.device_ids()}
            here = {known[d["udid"].lower()] for d in devices if d["reachable"] and d["udid"].lower() in known}
            with self.lock:
                arrived = here - self.here
                self.devices, self.here, self.anisette, self.muxer = devices, here, anisette, muxer
            for udid in sorted(arrived):
                self.on_arrival(udid)
        finally:
            with self.lock:
                self.scanning = False

    def scan_async(self):
        threading.Thread(target=self.scan, daemon=True).start()

    def loop(self):
        while True:
            self.scan()
            time.sleep(SCAN_INTERVAL)

    def visible(self, udid):
        with self.lock:
            return next((d for d in self.devices if d["udid"] == udid), None)

    def snapshot(self):
        with self.lock:
            return {"devices": list(self.devices), "anisette": self.anisette,
                    "muxer": self.muxer, "scanning": self.scanning}
