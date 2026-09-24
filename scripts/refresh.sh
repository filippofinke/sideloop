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
: "${ANISETTE_SERVER:=http://127.0.0.1:6969}"

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
FAILS="$APP_STATE/consecutive_failures"
LOG_FILE="$STATE_DIR/refresh.log"
NOISE='^(Signing|Installation) Progress:|^Writing File: |^Signing: |^(Data|Value) ?: |^X-(Apple|Mme|MMe)-|^(MachineID|One-Time Password|Local User ID|Device UDID|Device Description|Date|Sanitized client info) ?:|^Byte:|^(HMAC_OUT|NP):|anisette|^Received (auth )?response status code'

export ALTSERVER_ANISETTE_SERVER="$ANISETTE_SERVER"
export HOME="$DATA_DIR/.altserver"
mkdir -p "$STATE_DIR" "$APP_STATE" "$HOME"

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

probe_expiry() {
  local f tmp rc=1
  f="$(conn_flag)" || return 1
  tmp="$(mktemp -d)"
  if timeout 90 ideviceprovision ${f:+"$f"} -u "$DEVICE_UDID" copy "$tmp" >/dev/null 2>&1; then
    python3 - "$tmp" "$BUNDLE_ID" "${1:-0}" <<'PY' && rc=0 || rc=$?
import datetime, glob, plistlib, subprocess, sys
folder, bundle, min_created = sys.argv[1], sys.argv[2], int(sys.argv[3])
utc = lambda d: d.replace(tzinfo=datetime.timezone.utc).timestamp()
best = None
for p in glob.glob(folder + "/*.mobileprovision"):
    xml = subprocess.run(["openssl", "smime", "-verify", "-noverify", "-inform", "DER", "-in", p],
                         capture_output=True).stdout
    try:
        d = plistlib.loads(xml)
    except Exception:
        continue
    if bundle and bundle not in d.get("Entitlements", {}).get("application-identifier", ""):
        continue
    c, e = d.get("CreationDate"), d.get("ExpirationDate")
    if c and e and (best is None or c > best[0]):
        best = (c, int(utc(e)))
if best is None:
    sys.exit(1)
if utc(best[0]) < min_created:
    sys.exit(2)
print(best[1])
PY
  fi
  rm -rf "$tmp"
  return $rc
}

stamp_success() {
  local e
  e="$(probe_expiry $(( RUN_START - 300 )))" || return 1
  echo "$e" > "$EXPIRY"
  echo 0 > "$FAILS"
}

days_left() { [[ -r "$EXPIRY" ]] && echo $(( ( $(cat "$EXPIRY") - $(now) ) / 86400 )) || echo -999; }
read_fails() { local n=0; [[ -r "$FAILS" ]] && n="$(tr -cd '0-9' < "$FAILS")"; echo "${n:-0}"; }

if (( RECHECK )); then
  device_online || { echo "${TAG}${DEVICE_NAME} not reachable; unlock it and try again"; exit 1; }
  e="$(probe_expiry)" || { rm -f "$EXPIRY"; log "not installed on this device"; exit 0; }
  echo "$e" > "$EXPIRY"
  log "rechecked on the device: signature valid until $(fmt_epoch "$e")"
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
  exit 0
fi

hint=""
if (( rc == 0 )); then
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
exit "$rc"
