import unittest
from unittest import mock

from tests import FakeJob
from sideloop import pairing

UDID = "00008120-001A2B3C4D5E6F70"


class WaitForUsb(unittest.TestCase):
    def wait(self, answers, timeout=300):
        with mock.patch.object(pairing, "_sh", side_effect=answers), mock.patch.object(pairing.time, "sleep"), \
                mock.patch.object(pairing, "USB_TIMEOUT", timeout):
            return pairing._wait_for_usb(FakeJob())

    def test_muxer_error_is_not_taken_for_a_device(self):
        self.assertEqual(self.wait([(255, "ERROR: Unable to retrieve device list!"), (0, UDID)]), UDID)

    def test_timeout_reports_the_muxer_error(self):
        with self.assertRaisesRegex(RuntimeError, "Unable to retrieve device list"):
            self.wait([(255, "ERROR: Unable to retrieve device list!")], timeout=-1)


if __name__ == "__main__":
    unittest.main()
