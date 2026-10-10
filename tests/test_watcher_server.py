import http.client
import json
import threading
import time
import unittest
from unittest import mock

from tests import FakeJob, reset_data
from sideloop import auth, config, remote, store
from sideloop.config import APPS, write_env
from sideloop.server import App, Server, handler
from sideloop.watcher import Watcher

A = "00008101-000A1234ABCD001E"
B = "00008101-000B1234ABCD001E"


def make_app(app_id, devices, expiry=None):
    d = APPS / app_id
    d.mkdir(parents=True)
    (d / "app.ipa").write_bytes(b"")
    write_env(d / "meta.env", {"BUNDLE_ID": app_id, "APP_NAME": app_id, "DEVICES": " ".join(devices)})
    if expiry:
        for udid in devices:
            (d / "state" / udid).mkdir(parents=True)
            (d / "state" / udid / "expiry").write_text(str(int(expiry)))


def setup_two_devices():
    """A has an app that was never installed (due), B one valid for six more days (not due)."""
    reset_data()
    store.add_device(A, "Phone A")
    store.add_device(B, "Phone B")
    make_app("one", [A])
    make_app("two", [B], expiry=time.time() + 6 * 86400)


class WatcherDue(unittest.TestCase):
    def setUp(self):
        setup_two_devices()
        self.w = Watcher(jobs=None, health=None)

    def test_due_per_device(self):
        self.assertTrue(self.w.due(A))
        self.assertFalse(self.w.due(B))
        self.assertTrue(self.w.due())

    def test_due_matches_refresh_sh_whole_days(self):
        # refresh.sh refreshes when the whole days left are at most RENEW_BEFORE_DAYS (2 by default).
        for days_left, due in ((3.5, False), (2.99, True), (2.5, True), (0.5, True), (-1, True)):
            with self.subTest(days_left=days_left):
                (APPS / "two" / "state" / B / "expiry").write_text(str(int(time.time() + days_left * 86400)))
                self.assertEqual(self.w.due(B), due)

    def test_timer_pass_only_targets_due_devices(self):
        self.assertEqual(self.w.targets(True, []), [A])

    def test_arrivals_with_nothing_due_are_skipped(self):
        self.assertEqual(self.w.targets(False, [B]), [])
        self.assertEqual(self.w.targets(False, [A, B]), [A])


class WatcherCheckin(unittest.TestCase):
    def setUp(self):
        setup_two_devices()
        self.w = Watcher(jobs=None, health=None)

    def test_checkin_queues_a_due_device(self):
        self.assertIn("starting", self.w.checkin(A))
        self.assertEqual(self.w.queue, {A})

    def test_second_checkin_is_debounced(self):
        self.w.checkin(A)
        self.w.queue.clear()
        self.assertIn("already", self.w.checkin(A))
        self.assertEqual(self.w.queue, set())

    def test_checkin_with_nothing_due(self):
        self.assertIn("nothing is due", self.w.checkin(B))
        self.assertEqual(self.w.queue, set())

    def test_checkin_with_auto_check_off(self):
        config.update({"AUTO_CHECK": "0"})
        self.assertIn("turned off", self.w.checkin(A))
        self.assertEqual(self.w.queue, set())


class FakeHealth:
    def snapshot(self):
        return {"devices": [], "anisette": True, "muxer": True, "scanning": False}

    def is_local(self, udid):
        return False


class FakeJobs:
    def __init__(self):
        self.current = self.auto = None
        self.job = FakeJob()

    def busy(self):
        return False

    def start(self, kind, title, target):
        target(self.job)
        self.job.id = "job1"
        return self.job


class FakeWatcher:
    def __init__(self):
        self.checked_in = []

    def checkin(self, udid):
        self.checked_in.append(udid)
        return "a refresh is due; starting it"


class ServerTest(unittest.TestCase):
    def setUp(self):
        setup_two_devices()
        config.update({"APPLE_ID": "me@example.com", "APPLE_PASSWORD": "secret"})
        auth.set_password("password1")
        self.jobs, self.watcher = FakeJobs(), FakeWatcher()
        self.app = App(self.jobs, FakeHealth(), muxers=object(), watcher=self.watcher)
        self.srv = Server(("127.0.0.1", 0), handler(self.app))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.cookie = f"sideloop_session={auth.new_session()}"

    def request(self, method, path, body=None, cookie=True):
        c = http.client.HTTPConnection(*self.srv.server_address, timeout=5)
        headers = {"X-Requested-With": "sideloop"}
        if cookie:
            headers["Cookie"] = self.cookie
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        r = c.getresponse()
        data = json.loads(r.read() or b"{}")
        c.close()
        return r.status, data


class CheckinRoute(ServerTest):
    def test_valid_checkin_needs_no_session(self):
        status, data = self.request("GET", f"/api/checkin/{auth.checkin_token(A)}", cookie=False)
        self.assertEqual(status, 200)
        self.assertEqual(data["message"], "a refresh is due; starting it")
        self.assertEqual(self.watcher.checked_in, [A])

    def test_unknown_checkin_is_404(self):
        with mock.patch("sideloop.server.time.sleep"):
            status, _ = self.request("GET", "/api/checkin/" + "0" * 32, cookie=False)
            junk, _ = self.request("GET", "/api/checkin/%C3%A9", cookie=False)
        self.assertEqual((status, junk), (404, 404))
        self.assertEqual(self.watcher.checked_in, [])


class TunnelRoute(ServerTest):
    def test_needs_a_session(self):
        status, _ = self.request("POST", "/api/device/tunnel", {"device": A, "ip": "10.8.0.2"}, cookie=False)
        self.assertEqual(status, 401)

    def test_saves_a_valid_ip(self):
        status, _ = self.request("POST", "/api/device/tunnel", {"device": A, "ip": " 10.8.0.2 "})
        self.assertEqual(status, 200)
        self.assertEqual(store.device_tunnel_ip(A), "10.8.0.2")

    def test_rejects_garbage(self):
        status, data = self.request("POST", "/api/device/tunnel", {"device": A, "ip": "10.8.0.2:62078"})
        self.assertEqual(status, 400)
        self.assertIn("WireGuard IP", data["error"])
        self.assertEqual(store.device_tunnel_ip(A), "")

    def test_unavailable_without_builtin_muxers(self):
        self.app.muxers = None
        status, _ = self.request("POST", "/api/device/tunnel", {"device": A, "ip": "10.8.0.2"})
        self.assertEqual(status, 400)

    def test_state_lists_tunnel_ip_and_checkin_token(self):
        store.set_device_tunnel_ip(A, "10.8.0.2")
        dev = next(d for d in self.app.state()["registered"] if d["udid"] == A)
        self.assertEqual((dev["tunnel_ip"], dev["checkin"]), ("10.8.0.2", auth.checkin_token(A)))


class ManualRuns(ServerTest):
    def runs(self, method, data):
        with mock.patch.object(remote, "run_devices", return_value=0) as run:
            method(data)
        return [(c.args[1], c.args[2]) for c in run.call_args_list]

    def test_refresh_everything_goes_per_assigned_device(self):
        self.assertEqual(self.runs(self.app.refresh, {"force": True}), [([A, B], ["refresh.sh", "--force"])])

    def test_refresh_one_app_on_one_device(self):
        self.assertEqual(self.runs(self.app.refresh, {"app": "one", "device": A}), [([A], ["refresh.sh", "--app", "one"])])

    def test_recheck_goes_per_device(self):
        self.assertEqual(self.runs(self.app.recheck, {"app": "two"}), [([B], ["refresh.sh", "--recheck", "--app", "two"])])

    def test_nothing_assigned(self):
        store.set_app_devices("one", [])
        store.set_app_devices("two", [])
        self.assertEqual(self.runs(self.app.refresh, {}), [])
        self.assertIn("nothing to do", self.jobs.job.lines[-1])


if __name__ == "__main__":
    unittest.main()
