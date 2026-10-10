import threading
import time

from . import config, remote, store
from .config import APPS
from .jobs import run_capture
from .log import log

DUE_INTERVAL = 30 * 60
IDLE_INTERVAL = 6 * 60 * 60
ARRIVAL_DEBOUNCE = 5 * 60
RUN_TIMEOUT = 6 * 60 * 60


def configured(cfg=None):
    cfg = cfg or config.read()
    return bool(cfg["APPLE_ID"] and cfg["APPLE_PASSWORD"] and store.device_ids() and store.app_ids())


class Watcher:
    def __init__(self, jobs, health):
        self.jobs, self.health = jobs, health
        self.next_run = time.time() + 120
        self.last_poke = {}
        self.queue = set()
        self.lock = threading.Lock()

    def due(self, device=None):
        window = config.renew_before_days() * 86400
        for app_id in store.app_ids():
            for udid in store.app_devices(app_id):
                if device and udid != device:
                    continue
                expiry = store.read_state(APPS / app_id / "state" / udid)["expiry"]
                # Same rule as refresh.sh: due once the whole days left are at most RENEW_BEFORE_DAYS.
                if not expiry or expiry - time.time() < window + 86400:
                    return True
        return False

    def schedule(self):
        self.next_run = time.time() + (DUE_INTERVAL if self.due() else IDLE_INTERVAL)

    def poke(self, udid, why="came online"):
        now = time.time()
        with self.lock:
            if now - self.last_poke.get(udid, 0) < ARRIVAL_DEBOUNCE:
                return False
            self.last_poke[udid] = now
            self.queue.add(udid)
        log(f"watcher: {store.device_name(udid)} {why}")
        return True

    def checkin(self, udid):
        if config.read()["AUTO_CHECK"] != "1":
            return "automatic refreshing is turned off"
        if not self.due(udid):
            return "nothing is due"
        if not self.poke(udid, "checked in"):
            return "already checked in a moment ago"
        return "a refresh is due; starting it"

    def targets(self, timer, queued):
        return [u for u in (store.device_ids() if timer else queued) if self.due(u)]

    def loop(self):
        while True:
            time.sleep(5)
            timer = time.time() >= self.next_run
            with self.lock:
                queued = sorted(self.queue)
            if not timer and not queued:
                continue
            cfg = config.read()
            if cfg["AUTO_CHECK"] != "1" or not configured(cfg):
                with self.lock:
                    self.queue.clear()
                self.schedule()
                continue
            job = self.jobs.claim_auto()
            if not job:
                continue
            with self.lock:
                self.queue.clear()
            try:
                job.rc = remote.run_devices(job, self.targets(timer, queued), ["refresh.sh"],
                                            lambda j, argv, env: run_capture(j, argv, RUN_TIMEOUT, env),
                                            self.health.is_local)
            finally:
                job.ended = time.time()
                if job.lines:
                    log("watcher:", job.lines[-1])
                self.schedule()
                self.health.scan()
