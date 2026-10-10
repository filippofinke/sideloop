import hashlib
import hmac
import json
import os
import re
import secrets
import time

from .config import UI_FILE

SESSION_TTL = 30 * 86400
TOKEN_RE = re.compile(r"[0-9a-f]{32}")


def _load():
    try:
        return json.loads(UI_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


def _sign(secret, value):
    return hmac.new(secret.encode(), value.encode(), "sha256").hexdigest()


def has_password():
    return bool(_load())


def set_password(password):
    salt = secrets.token_hex(16)
    UI_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(UI_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"salt": salt, "hash": _hash(password, salt), "secret": secrets.token_hex(32)}, f)


def check_password(password):
    c = _load()
    return bool(c) and hmac.compare_digest(_hash(password, c["salt"]), c["hash"])


def new_session():
    exp = str(int(time.time()) + SESSION_TTL)
    return f"{exp}.{_sign(_load()['secret'], exp)}"


def valid_session(token):
    c = _load()
    if not c or not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    return hmac.compare_digest(sig, _sign(c["secret"], exp)) and exp.isdigit() and int(exp) > time.time()


def checkin_token(udid):
    c = _load()
    return _sign(c["secret"], f"checkin:{udid}")[:32] if c else ""


def checkin_device(token, udids):
    if not TOKEN_RE.fullmatch(token or ""):
        return None
    return next((u for u in udids if hmac.compare_digest(checkin_token(u), token)), None)
