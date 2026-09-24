import os
import pty
import re
import secrets
import select
import signal
import subprocess
import threading
import time

from . import config

MASK = "••••••••"
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
PROMPT = re.compile(r"two.?factor code|verification code|enter (the )?(2fa |verification )?code", re.I)
SECRET = re.compile(r"^(Data|Value) ?: |^X-(Apple|Mme|MMe)-|^(MachineID|One-Time Password|Local User ID|Device UDID|"
                    r"Device Description|Date|Sanitized client info) ?:|^Byte:|^(HMAC_OUT|NP):|anisette|"
                    r"^Received (auth )?response|^Signing: ")
PROGRESS = re.compile(r"^(Signing|Installation) Progress:\s*([0-9.eE+-]+)")
NOISE = re.compile(r"^Writing File: ")
APP_START = re.compile(r"\[(.+?)\] (?:signing and installing|no signature on record|signature has)")
MAX_LINE = 400


def hidden(line):
    return bool(PROGRESS.match(line) or NOISE.match(line) or SECRET.search(line) or len(line) > MAX_LINE)


def redact(lines):
    secret = config.read()["APPLE_PASSWORD"]
    return [ln.replace(secret, MASK) if secret else ln for ln in lines if not hidden(ln)]


class Job:
    def __init__(self, kind, title):
        self.id, self.kind, self.title = secrets.token_hex(4), kind, title
        self.started, self.ended, self.rc = time.time(), None, None
        self.lines, self.partial, self.needs_input = [], "", False
        self.stage, self.fraction, self.app = None, 0.0, None
        self.pid, self.fd, self.cancelled = None, None, False
        self.lock = threading.Lock()
        self.secret = config.read()["APPLE_PASSWORD"]

    @property
    def running(self):
        return self.ended is None

    def add(self, text):
        text = ANSI.sub("", text)
        if self.secret:
            text = text.replace(self.secret, MASK)
        with self.lock:
            buf = (self.partial + text).replace("\r\n", "\n").replace("\r", "\n")
            *done, self.partial = buf.split("\n")
            for line in done:
                t = line.strip()
                if not t:
                    continue
                self._track(t)
                if hidden(t):
                    continue
                self.lines.append(line.rstrip())
                if len(t) < 160 and PROMPT.search(t):
                    self.needs_input = True
            if len(self.partial) < 160 and PROMPT.search(self.partial):
                self.needs_input = True
            self.lines = self.lines[-1500:]

    def _track(self, line):
        if line.startswith(("Got token for", "Fetching team")):
            self.needs_input = False
        if m := PROGRESS.match(line):
            try:
                f = max(0.0, min(1.0, float(m.group(2))))
            except ValueError:
                return
            stage = "sign" if m.group(1) == "Signing" else "send"
            if stage == "send" and (f >= 0.999 or self.stage == "install"):
                stage = "install"
            self.stage, self.fraction, self.needs_input = stage, f, False
        elif line.startswith("Writing to device"):
            self.stage, self.fraction = "send", 0.0
        elif line.startswith(("Installed profile", "Notify: Installation")):
            self.stage, self.fraction = "install", 1.0
        elif "signing and installing" in line:
            if m := APP_START.search(line):
                self.app = m.group(1)
            self.stage, self.fraction = "signin", 0.0

    def say(self, line):
        self.add(line + "\n")

    def send(self, text):
        if self.fd is None:
            raise RuntimeError("this step doesn't take input")
        with self.lock:
            self.needs_input = False
            self.lines.append((self.partial.strip() + " ••••••").strip())
            self.partial = ""
        os.write(self.fd, (text.strip() + "\n").encode())

    def cancel(self):
        self.cancelled = True
        if self.pid:
            try:
                os.killpg(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def view(self, since=None):
        with self.lock:
            v = {"id": self.id, "kind": self.kind, "title": self.title, "started": int(self.started),
                 "ended": int(self.ended) if self.ended else None, "rc": self.rc, "running": self.running,
                 "needs_input": self.needs_input, "accepts_input": self.fd is not None,
                 "stage": self.stage, "fraction": round(self.fraction, 4), "app": self.app,
                 "total": len(self.lines), "partial": self.partial.strip(),
                 "last": self.lines[-1] if self.lines else ""}
            if since is not None:
                v["lines"] = self.lines[since:]
            return v


class Jobs:
    def __init__(self, on_done):
        self.lock = threading.Lock()
        self.current = None
        self.auto = None
        self.on_done = on_done

    def busy(self):
        return any(j and j.running for j in (self.current, self.auto))

    def start(self, kind, title, target):
        with self.lock:
            if self.current and self.current.running:
                raise RuntimeError("something is already running")
            if self.auto and self.auto.running:
                raise RuntimeError(f"{self.auto.app or 'an automatic check'} is running automatically; "
                                   "try again when it's done")
            job = self.current = Job(kind, title)
        threading.Thread(target=self._run, args=(job, target), daemon=True).start()
        return job

    def _run(self, job, target):
        try:
            target(job)
        except Exception as e:
            job.say(f"✗ {e}")
            job.rc = job.rc or 1
        finally:
            job.rc = job.rc or 0
            job.ended = time.time()
            self.on_done()

    def claim_auto(self):
        with self.lock:
            if self.busy():
                return None
            self.auto = Job("auto", "Automatic check")
            return self.auto


def run_pty(job, argv):
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execvp(argv[0], argv)
        finally:
            os._exit(127)
    job.pid, job.fd = pid, fd
    status = None
    try:
        while True:
            if select.select([fd], [], [], 0.5)[0]:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                job.add(data.decode("utf-8", "replace"))
            elif status is None:
                wp, st = os.waitpid(pid, os.WNOHANG)
                if wp:
                    status = st
    finally:
        job.fd = None
        os.close(fd)
    if status is None:
        status = os.waitpid(pid, 0)[1]
    job.rc = os.waitstatus_to_exitcode(status)
    if job.partial.strip():
        job.say("")


def run_capture(job, argv, timeout=None):
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         start_new_session=True)
    job.pid = p.pid
    deadline = time.time() + timeout if timeout else None
    for chunk in iter(lambda: p.stdout.read1(4096), b""):
        job.add(chunk.decode("utf-8", "replace"))
        if deadline and time.time() > deadline:
            os.killpg(p.pid, signal.SIGKILL)
            job.say("✗ timed out")
            break
    job.rc = p.wait()
