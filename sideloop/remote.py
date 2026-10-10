import ipaddress
import os
import socket
import subprocess
import time
from contextlib import ExitStack, contextmanager

from . import store
from .config import LOCKDOWN_DIR, TUNNEL_MUX, WG_GATEWAY, WG_SUBNET
from .lockdown import LOCKDOWN_PORT, mux_connect, mux_request, mux_send
from .log import log

# Bonjour doesn't cross a VPN, so a device away from home is handed to a second netmuxd by its
# WireGuard IP for the length of one run.
PROBE_TIMEOUT = 3
MUXER_START_TIMEOUT = 10
SERVICE_NAME = "_apple-mobdev2._tcp.local"


class TunnelError(Exception):
    pass


def add_route(verbose=False):
    if not (WG_SUBNET and WG_GATEWAY):
        if verbose and (WG_SUBNET or WG_GATEWAY):
            log("set both WG_SUBNET and WG_GATEWAY to route to the WireGuard subnet")
        return
    r = subprocess.run(["ip", "route", "replace", WG_SUBNET, "via", WG_GATEWAY], capture_output=True, text=True)
    if r.returncode:
        log(f"couldn't route {WG_SUBNET} via {WG_GATEWAY}: {r.stderr.strip() or r.returncode}")
    elif verbose:
        log(f"routing {WG_SUBNET} via {WG_GATEWAY} to reach devices away from home")


def valid_ip(text):
    text = str(text or "").strip()
    if not text:
        return ""
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        ip = None
    if ip is None or getattr(ip, "scope_id", None):
        raise ValueError("enter the device's WireGuard IP, like 10.8.0.2")
    return str(ip)


def reachable(ip, port=LOCKDOWN_PORT):
    try:
        socket.create_connection((ip, port), timeout=PROBE_TIMEOUT).close()
        return True
    except OSError:
        return False


def _remove(udid, mux):
    try:
        with mux_connect(mux) as s:
            mux_send(s, {"MessageType": "RemoveDevice", "DeviceID": udid})
            s.recv(1)  # netmuxd closes the connection once it has queued the removal
    except OSError:
        pass


def tunnel_cmd(port):
    # With heartbeat: over the network iOS closes every service right after its TLS handshake
    # unless the host holds a heartbeat session with the device.
    return ["netmuxd", "--host", "127.0.0.1", "-p", port, "--disable-unix", "--disable-mdns",
            "--disable-usb", "--plist-storage", LOCKDOWN_DIR]


@contextmanager
def tunnel_muxer():
    # netmuxd only drops a heartbeat when it exits, so each run gets its own and stops it afterwards;
    # between runs nothing keeps the phone's radio awake.
    host, port = TUNNEL_MUX.rsplit(":", 1)
    if reachable(host, int(port)):
        raise TunnelError(f"something else is listening on {TUNNEL_MUX}")
    try:
        p = subprocess.Popen(tunnel_cmd(port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise TunnelError(f"couldn't start the tunnel muxer ({e})") from None
    try:
        deadline = time.time() + MUXER_START_TIMEOUT
        while not reachable(host, int(port)):
            if p.poll() is not None or time.time() > deadline:
                raise TunnelError("the tunnel muxer didn't start")
            time.sleep(0.1)
        yield TUNNEL_MUX
    finally:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


@contextmanager
def session(udid, ip, mux=None):
    with ExitStack() as stack:
        mux = mux or stack.enter_context(tunnel_muxer())
        _remove(udid, mux)
        try:
            with mux_connect(mux) as s:
                r = mux_request(s, {"MessageType": "AddDevice", "ConnectionType": "Network",
                                    "ServiceName": SERVICE_NAME, "IPAddress": ip, "DeviceID": udid})
            ok = r.get("Result") == 1
        except Exception as e:
            raise TunnelError(f"the tunnel muxer didn't take {store.device_name(udid)} ({e}); is it paired?") from None
        if not ok:
            raise TunnelError(f"the tunnel muxer refused {store.device_name(udid)} ({r}); "
                              "it couldn't start a heartbeat with the device")
        try:
            yield dict(os.environ, USBMUXD_SOCKET_ADDRESS=mux)
        finally:
            _remove(udid, mux)


def run_devices(job, udids, argv, runner, is_local):
    rc = 0
    for udid in udids:
        if job.cancelled:
            break
        cmd = [*argv, "--device", udid]
        ip = store.device_tunnel_ip(udid)
        result = None
        if ip and not is_local(udid):
            add_route()
            if reachable(ip):
                job.say(f"{store.device_name(udid)} is away from home; reaching it over WireGuard at {ip}")
                log(f"reaching {store.device_name(udid)} over WireGuard at {ip}")
                try:
                    with session(udid, ip) as env:
                        if not job.cancelled:
                            result = runner(job, cmd, env)
                except TunnelError as e:
                    job.say(f"✗ {e}")
        if job.cancelled:
            break
        if result is None:
            result = runner(job, cmd, None)
        rc = rc or result
    return rc
