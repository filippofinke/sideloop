import os
import plistlib
import socket
import ssl
import struct
import tempfile

LOCKDOWN_PORT = 62078
WIFI_DOMAIN = "com.apple.mobile.wireless_lockdown"
WIFI_KEY = "EnableWiFiConnections"


def _mux_connect(addr):
    if ":" in addr:
        host, port = addr.rsplit(":", 1)
        return socket.create_connection((host, int(port)), timeout=15)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(15)
    s.connect(addr)
    return s


def _recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


def _mux_request(s, msg, tag=1):
    body = plistlib.dumps(dict(msg, ClientVersionString="sideloop", ProgName="sideloop"))
    s.sendall(struct.pack("<IIII", 16 + len(body), 1, 8, tag) + body)
    length = struct.unpack("<I", _recv_exact(s, 4))[0]
    _recv_exact(s, 12)
    return plistlib.loads(_recv_exact(s, length - 16))


def _device_id(udid, mux):
    with _mux_connect(mux) as s:
        for d in _mux_request(s, {"MessageType": "ListDevices"}).get("DeviceList", []):
            if d["Properties"].get("SerialNumber", "").lower() == udid.lower():
                return d["DeviceID"]
    raise LookupError(f"device {udid} not visible to the muxer")


def _pair_record(udid, mux):
    with _mux_connect(mux) as s:
        r = _mux_request(s, {"MessageType": "ReadPairRecord", "PairRecordID": udid})
    if "PairRecordData" not in r:
        raise LookupError("no pairing record: pair the device first")
    return plistlib.loads(r["PairRecordData"])


class Lockdown:
    def __init__(self, udid, mux):
        self.pair = _pair_record(udid, mux)
        s = _mux_connect(mux)
        port = struct.unpack(">H", struct.pack("<H", LOCKDOWN_PORT))[0]
        r = _mux_request(s, {"MessageType": "Connect", "DeviceID": _device_id(udid, mux), "PortNumber": port})
        if r.get("Number", 1) != 0:
            s.close()
            raise ConnectionError(f"muxer refused the lockdown connection ({r.get('Number')})")
        self.sock = s
        r = self._call({"Request": "StartSession", "HostID": self.pair["HostID"],
                        "SystemBUID": self.pair["SystemBUID"]})
        if "Error" in r:
            raise PermissionError(f"lockdown StartSession: {r['Error']}")
        self.session = r["SessionID"]
        if r.get("EnableSessionSSL"):
            self._start_tls()

    def _start_tls(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        with tempfile.TemporaryDirectory() as d:
            cert, key = os.path.join(d, "c"), os.path.join(d, "k")
            with open(cert, "wb") as f:
                f.write(self.pair["HostCertificate"])
            with open(key, "wb") as f:
                f.write(self.pair["HostPrivateKey"])
            ctx.load_cert_chain(cert, key)
        self.sock = ctx.wrap_socket(self.sock)

    def _call(self, req):
        body = plistlib.dumps(dict(req, Label="sideloop"))
        self.sock.sendall(struct.pack(">I", len(body)) + body)
        return plistlib.loads(_recv_exact(self.sock, struct.unpack(">I", _recv_exact(self.sock, 4))[0]))

    def get(self, domain, key):
        r = self._call({"Request": "GetValue", "Domain": domain, "Key": key})
        if r.get("Error") == "MissingValue":
            return None
        if "Error" in r:
            raise RuntimeError(r["Error"])
        return r.get("Value")

    def set(self, domain, key, value):
        r = self._call({"Request": "SetValue", "Domain": domain, "Key": key, "Value": value})
        if "Error" in r:
            raise RuntimeError(r["Error"])

    def close(self):
        try:
            self._call({"Request": "StopSession", "SessionID": self.session})
        except Exception:
            pass
        self.sock.close()


def enable_wifi(udid, mux):
    ld = Lockdown(udid, mux)
    try:
        ld.set(WIFI_DOMAIN, WIFI_KEY, True)
        return bool(ld.get(WIFI_DOMAIN, WIFI_KEY))
    finally:
        ld.close()
