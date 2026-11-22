#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
CONFIG="$DATA_DIR/config.env"
APPS_DIR="$DATA_DIR/apps"
DEVICES_DIR="$DATA_DIR/devices"
STATE_DIR="$DATA_DIR/state"
ALTSERVER_BIN="${ALTSERVER_BIN:-/usr/local/bin/AltServer}"

[[ -r "$CONFIG" ]] || { echo "missing config: $CONFIG" >&2; exit 78; }
set -a; . "$CONFIG"; set +a
: "${APPLE_ID:?}" "${APPLE_PASSWORD:?}"
: "${RENEW_BEFORE_DAYS:=2}"
: "${ANISETTE_SERVER:=https://new.ani.sidestore.io}"

FORCE=0; RECHECK=0; APP=""; DEVICE=""
while (( $# )); do
  case "$1" in
    --force) FORCE=1 ;;
    --recheck) RECHECK=1 ;;
    --app) APP="${2:?--app needs an id}"; shift ;;
    --device) DEVICE="${2:?--device needs a udid}"; shift ;;
  esac
  shift
done

if [[ -z "$APP" || -z "$DEVICE" ]]; then
  rc=0; n=0
  for d in "$APPS_DIR"/*/; do
    [[ -r "$d/meta.env" && -r "$d/app.ipa" ]] || continue
    id="$(basename "$d")"
    [[ -z "$APP" || "$APP" == "$id" ]] || continue
    for u in $(set -a; . "$d/meta.env"; printf '%s' "${DEVICES:-}"); do
      [[ -z "$DEVICE" || "${DEVICE,,}" == "${u,,}" ]] || continue
      [[ -r "$DEVICES_DIR/$u.env" ]] || continue
      n=$((n+1))
      args=(--app "$id" --device "$u")
      (( FORCE )) && args+=(--force)
      (( RECHECK )) && args+=(--recheck)
      "${BASH_SOURCE[0]}" "${args[@]}" || rc=$?
    done
  done
  (( n )) || echo "nothing to do: no app is assigned to ${DEVICE:-any device}"
  exit "$rc"
fi

APP_DIR="$APPS_DIR/$APP"
[[ -r "$APP_DIR/meta.env" ]] || { echo "unknown app: $APP" >&2; exit 78; }
set -a; . "$APP_DIR/meta.env"; set +a
DEVICE_UDID="$DEVICE"
DEVICE_NAME="$DEVICE"
[[ -r "$DEVICES_DIR/$DEVICE.env" ]] && { set -a; . "$DEVICES_DIR/$DEVICE.env"; set +a; }
IPA_PATH="$APP_DIR/app.ipa"
APP_STATE="$APP_DIR/state/$DEVICE"
TAG="[${APP_NAME:-$APP} on $DEVICE_NAME] "
EXPIRY="$APP_STATE/expiry"
CERT="$APP_STATE/certificate"
FAILS="$APP_STATE/consecutive_failures"
LOG_FILE="$STATE_DIR/refresh.log"
NOISE='^(Signing|Installation) Progress:|^Writing File: |^Signing: |^(Data|Value) ?: |^X-(Apple|Mme|MMe)-|^(MachineID|One-Time Password|Local User ID|Device UDID|Device Description|Date|Sanitized client info) ?:|^Byte:|^(HMAC_OUT|NP):|anisette|^Received (auth )?response status code'

export ALTSERVER_ANISETTE_SERVER="$ANISETTE_SERVER"
# Stock AltServer removes every free provisioning profile on the device before installing. Once the
# last profile of a developer is gone iOS forgets that the user trusted it, so every refresh brought
# back "Untrusted Developer". The patched AltServer leaves them in place, and prune_profiles below
# removes the outdated ones afterwards, never the newest.
export ALTSERVER_KEEP_PROFILES=1
# Repeated refused sign-ins can get the Apple ID locked, so automatic runs stop after a few of them.
# A manual run from the web UI uses --force, still signs in, and clears the count when it succeeds.
SIGNIN_FAILS="$STATE_DIR/signin_failures"
MAX_SIGNIN_FAILS=3
SIGNIN_ERROR='Error: com\.rileytestut\.(ALTAppleAPI \(|AltServer\.Localized \(-[0-9]{4,})'
export HOME="$DATA_DIR/.altserver"
mkdir -p "$STATE_DIR" "$APP_STATE" "$HOME"
# AltServer caches its signing certificate in ./AltServerData. Without that cache it revokes the
# certificate and makes a new one, and iOS then asks to trust the developer again.
cd "$DATA_DIR"

now() { date +%s; }
fmt_epoch() { date -d "@$1" '+%a %d %b %H:%M'; }
log() {
  local line; line="$(date '+%Y-%m-%d %H:%M:%S') $TAG$*"
  printf '%s\n' "$line" | tee -a "$LOG_FILE"
}
record() { printf '%s\t%s\t%s\n' "$(now)" "$1" "$2" > "$APP_STATE/last_run"; }

exec 9>"$STATE_DIR/.lock"
if ! flock -n 9; then
  echo "${TAG}waiting for $(cat "$STATE_DIR/.running" 2>/dev/null || echo "another run") to finish…"
  flock -w 1800 9 || { echo "${TAG}gave up waiting after 30 minutes"; exit 1; }
fi
printf '%s' "${TAG% }" > "$STATE_DIR/.running"
trap 'rm -f "$STATE_DIR/.running"' EXIT
RUN_START="$(now)"

conn_flag() {
  if timeout 10 idevice_id -l 2>/dev/null | grep -qi "^$DEVICE_UDID"; then echo ""
  elif timeout 10 idevice_id -n 2>/dev/null | grep -qi "^$DEVICE_UDID"; then printf '%s\n' -n
  else return 1; fi
}

device_online() {
  local f; f="$(conn_flag)" || return 1
  timeout 15 ideviceinfo ${f:+"$f"} -u "$DEVICE_UDID" -k DeviceName >/dev/null 2>&1
}

# Copies the device's provisioning profiles into a new temporary folder and prints its path.
copy_profiles() {
  local f tmp
  f="$(conn_flag)" || return 1
  tmp="$(mktemp -d)"
  if ! timeout 90 ideviceprovision ${f:+"$f"} -u "$DEVICE_UDID" copy "$tmp" >/dev/null 2>&1; then
    rm -rf "$tmp"; return 1
  fi
  echo "$tmp"
}

# Prints "<expiry> <certificate sha1>" of the newest profile in a folder for the main app of a
# bundle. Exits 1 when there is none, 2 when it is older than min_created. With a certificate
# sha1, profiles signed by another certificate (e.g. SideStore with another Apple ID) are skipped.
read_profile() {
  python3 - "$1" "$2" "${3:-0}" "${4:-}" <<'PY'
import datetime, glob, hashlib, plistlib, re, subprocess, sys
folder, bundle, min_created, cert = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
utc = lambda d: d.replace(tzinfo=datetime.timezone.utc).timestamp()
main_app = re.compile(r"^(\w+)\." + re.escape(bundle) + r"\.\1$")
best = None
for p in glob.glob(folder + "/*.mobileprovision"):
    xml = subprocess.run(["openssl", "smime", "-verify", "-noverify", "-inform", "DER", "-in", p],
                         capture_output=True).stdout
    try:
        d = plistlib.loads(xml)
    except Exception:
        continue
    if bundle and not main_app.match(d.get("Entitlements", {}).get("application-identifier", "")):
        continue
    c, e = d.get("CreationDate"), d.get("ExpirationDate")
    certs = [hashlib.sha1(x).hexdigest() for x in d.get("DeveloperCertificates") or [b""]]
    if cert and cert not in certs:
        continue
    if c and e and (best is None or c > best[0]):
        best = (c, int(utc(e)), certs[0])
if best is None:
    sys.exit(1)
if utc(best[0]) < min_created:
    sys.exit(2)
print(best[1], best[2])
PY
}

probe_expiry() {
  local tmp rc=0
  tmp="$(copy_profiles)" || return 1
  read_profile "$tmp" "$BUNDLE_ID" "${1:-0}" || rc=$?
  rm -rf "$tmp"
  return $rc
}

# AltServer removes the provisioning profiles of every sideloaded app on the device before it
# installs, and puts the others back only when the install succeeds. A run that fails or times
# out halfway leaves those apps without a profile, so iOS refuses to open them with "Untrusted
# Developer" even though their signature looks valid here. Check the other apps on the device
# after every run, and forget the signature of any that lost its profile so it gets reinstalled.
heal_siblings() {
  local tmp d s p name bundle cert
  tmp="$(copy_profiles)" || return 0
  for d in "$APPS_DIR"/*/; do
    s="$d/state/$DEVICE"
    [[ "$(basename "$d")" != "$APP" && -r "$s/expiry" && -r "$d/meta.env" ]] || continue
    IFS=$'\t' read -r bundle name < <(set -a; . "$d/meta.env"; printf '%s\t%s\n' "${BUNDLE_ID:-}" "${APP_NAME:-}")
    [[ -n "$bundle" ]] || continue
    cert=""; [[ -r "$s/certificate" ]] && cert="$(cat "$s/certificate")"
    if p="$(read_profile "$tmp" "$bundle" 0 "$cert")"; then
      echo "${p%% *}" > "$s/expiry"
    else
      rm -f "$s/expiry"
      log "WARNING ${name:-$bundle} lost its provisioning profile during this run and won't open;" \
          "it will be reinstalled on the next run"
    fi
  done
  rm -rf "$tmp"
}

# Removes provisioning profiles of this Apple ID's team that a newer profile for the same app or
# extension replaced, on every app of the team. The newest one always stays, so the developer remains trusted.
prune_profiles() {
  local f tmp u
  f="$(conn_flag)" || return 0
  tmp="$(copy_profiles)" || return 0
  python3 - "$tmp" "$BUNDLE_ID" <<'PY' | while read -r u; do
import glob, plistlib, re, subprocess, sys
folder, bundle = sys.argv[1], sys.argv[2]
profiles = []
for p in glob.glob(folder + "/*.mobileprovision"):
    xml = subprocess.run(["openssl", "smime", "-verify", "-noverify", "-inform", "DER", "-in", p],
                         capture_output=True).stdout
    try:
        d = plistlib.loads(xml)
    except Exception:
        continue
    profiles.append((d.get("Entitlements", {}).get("application-identifier", ""), d["CreationDate"], d["UUID"]))
main = re.compile(r"^(\w+)\." + re.escape(bundle) + r"\.\1$")
# The app was just installed, so its newest profile belongs to this Apple ID's team. Profiles of
# other teams (SideStore, Xcode) are left alone.
ours = max(((c, a) for a, c, _ in profiles if main.match(a)), default=None)
if ours is None:
    sys.exit()
team = ours[1].split(".", 1)[0]
newest = {}
for a, c, u in profiles:
    if a.split(".", 1)[0] == team and (a not in newest or c > newest[a][0]):
        newest[a] = (c, u)
for a, c, u in profiles:
    if a in newest and newest[a][1] != u:
        print(u)
PY
    timeout 30 ideviceprovision ${f:+"$f"} -u "$DEVICE_UDID" remove "$u" </dev/null >/dev/null 2>&1 || true
  done
  rm -rf "$tmp"
}

save_probe() {
  local e c old=""
  read -r e c <<< "$1"
  echo "$e" > "$EXPIRY"
  [[ -r "$CERT" ]] && old="$(cat "$CERT")"
  echo "$c" > "$CERT"
  if [[ -n "$old" && "$old" != "$c" ]]; then
    log "WARNING the signing certificate changed, so iOS will ask you to trust the developer again" \
        "(Settings > General > VPN & Device Management). It changes when $APPLE_ID is also used by" \
        "Xcode, AltStore, SideStore or Sideloadly, or when $DATA_DIR/AltServerData is lost."
  fi
}

stamp_success() {
  local p
  p="$(probe_expiry $(( RUN_START - 300 )))" || return 1
  save_probe "$p"
  echo 0 > "$FAILS"
  rm -f "$SIGNIN_FAILS"
}

days_left() { [[ -r "$EXPIRY" ]] && echo $(( ( $(cat "$EXPIRY") - $(now) ) / 86400 )) || echo -999; }
read_fails() { local n=0; [[ -r "$FAILS" ]] && n="$(tr -cd '0-9' < "$FAILS")"; echo "${n:-0}"; }
read_signin_fails() { local n=0; [[ -r "$SIGNIN_FAILS" ]] && n="$(tr -cd '0-9' < "$SIGNIN_FAILS")"; echo "${n:-0}"; }

if (( RECHECK )); then
  device_online || { echo "${TAG}${DEVICE_NAME} not reachable; unlock it and try again"; exit 1; }
  p="$(probe_expiry)" || { rm -f "$EXPIRY"; log "not installed on this device"; exit 0; }
  save_probe "$p"
  log "rechecked on the device: signature valid until $(fmt_epoch "$(cat "$EXPIRY")")"
  exit 0
fi

LEFT="$(days_left)"
(( FORCE == 0 && LEFT > RENEW_BEFORE_DAYS )) && exit 0

if [[ -r "$EXPIRY" ]]; then
  log "signature has ${LEFT}d left (threshold ${RENEW_BEFORE_DAYS}d)$( (( FORCE )) && echo ", forced run" )"
else
  log "no signature on record yet, doing the first install"
fi

if ! device_online; then
  log "${DEVICE_NAME} not reachable (asleep, locked, or off this Wi-Fi); will retry next run"
  record offline "device not reachable"
  exit 0
fi

[[ -r "$IPA_PATH" ]] || { log "ERROR IPA missing: $IPA_PATH"; record fail "IPA missing"; exit 1; }

if (( FORCE == 0 && $(read_signin_fails) >= MAX_SIGNIN_FAILS )); then
  log "Apple refused the last $(read_signin_fails) sign-ins; automatic runs are paused to protect the account, sign from the web UI"
  record fail "automatic runs paused after refused sign-ins; sign from the web UI"
  exit 0
fi

log "signing and installing $(basename "$IPA_PATH") on $DEVICE_UDID"
out="$(mktemp)"
trap 'rm -f "$STATE_DIR/.running" "$out"' EXIT
set +e
if [[ -t 0 && -t 1 ]]; then
  timeout --foreground 2400 "$ALTSERVER_BIN" -u "$DEVICE_UDID" -a "$APPLE_ID" -p "$APPLE_PASSWORD" "$IPA_PATH"
  rc=$?
else
  timeout 2400 "$ALTSERVER_BIN" -u "$DEVICE_UDID" -a "$APPLE_ID" -p "$APPLE_PASSWORD" "$IPA_PATH" </dev/null 2>&1 \
    | tee "$out"
  rc=${PIPESTATUS[0]}
  tr '\r' '\n' < "$out" | grep -vE "$NOISE" | cut -c1-400 >> "$LOG_FILE"
fi
set -e

if (( rc == 0 )) && stamp_success; then
  msg="refreshed, valid until $(fmt_epoch "$(cat "$EXPIRY")")"
  log "OK $msg"; record ok "$msg"
  prune_profiles
  heal_siblings
  exit 0
fi

hint=""
if grep -qE "$SIGNIN_ERROR" "$out"; then
  (( rc )) || rc=1
  hint="Apple refused the sign-in"
  echo $(( $(read_signin_fails) + 1 )) > "$SIGNIN_FAILS"
elif (( rc == 0 )); then
  rc=1
  hint="AltServer finished, but the app isn't on the device (did it lock or leave Wi-Fi mid-copy?)"
elif grep -qiE 'two.?factor code|verification code' "$out"; then
  hint="Apple is asking for a 2FA code; sign it from the web UI once"
elif (( rc == 124 )); then
  hint="AltServer timed out"
fi
n=$(( $(read_fails) + 1 )); echo "$n" > "$FAILS"
log "FAIL AltServer exited $rc (consecutive failures: $n)${hint:+ - $hint}"
record fail "AltServer exited $rc${hint:+: $hint}"
heal_siblings
exit "$rc"
