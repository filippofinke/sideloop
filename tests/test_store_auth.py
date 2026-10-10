import unittest

from tests import reset_data
from sideloop import auth, store
from sideloop.config import APPS, write_env

A = "00008101-000A1234ABCD001E"
B = "00008101-000B1234ABCD001E"
C = "00008101-000C1234ABCD001E"


def make_app(app_id, devices):
    d = APPS / app_id
    d.mkdir(parents=True)
    (d / "app.ipa").write_bytes(b"")
    write_env(d / "meta.env", {"BUNDLE_ID": app_id, "DEVICES": " ".join(devices)})


class TunnelIp(unittest.TestCase):
    def setUp(self):
        reset_data()
        store.add_device(A, "Phone")

    def test_tunnel_ip_round_trip(self):
        self.assertEqual(store.device_tunnel_ip(A), "")
        store.set_device_tunnel_ip(A, "10.8.0.2")
        self.assertEqual(store.device_tunnel_ip(A), "10.8.0.2")
        store.set_device_tunnel_ip(A, "")
        self.assertEqual(store.device_tunnel_ip(A), "")

    def test_add_device_keeps_tunnel_ip(self):
        store.set_device_tunnel_ip(A, "10.8.0.2")
        store.add_device(A, "Renamed Phone")
        self.assertEqual(store.device_tunnel_ip(A), "10.8.0.2")
        self.assertEqual(store.device_name(A), "Renamed Phone")


class AssignedDevices(unittest.TestCase):
    def setUp(self):
        reset_data()
        for udid in (A, B, C):
            store.add_device(udid, udid)
        make_app("one", [A])
        make_app("two", [B, A])

    def test_all_devices_with_an_app(self):
        self.assertEqual(store.assigned_devices(), [A, B])

    def test_devices_of_one_app(self):
        self.assertEqual(store.assigned_devices("one"), [A])


class CheckinToken(unittest.TestCase):
    def setUp(self):
        reset_data()

    def test_no_token_before_a_password_exists(self):
        self.assertEqual(auth.checkin_token(A), "")
        self.assertIsNone(auth.checkin_device("0" * 32, [A]))

    def test_token_is_stable_and_per_device(self):
        auth.set_password("password1")
        token = auth.checkin_token(A)
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        self.assertEqual(auth.checkin_token(A), token)
        self.assertNotEqual(auth.checkin_token(B), token)
        self.assertEqual(auth.checkin_device(auth.checkin_token(B), [A, B]), B)

    def test_checkin_device_rejects_junk(self):
        auth.set_password("password1")
        token = auth.checkin_token(A)
        for bad in ("", token[:-1], token + "0", token.upper(), "z" * 32, "é" * 32, "../" * 11):
            with self.subTest(bad=bad):
                self.assertIsNone(auth.checkin_device(bad, [A]))


if __name__ == "__main__":
    unittest.main()
