#!/usr/bin/env bash
set -uo pipefail
export HOME="${DATA_DIR:-/data}/.altserver"
mkdir -p "$HOME" 2>/dev/null

emit() {
  local name model ios ok=yes
  name="$(timeout 10 ideviceinfo $2 -u "$1" -k DeviceName 2>/dev/null)" || ok=no
  model="$(timeout 10 ideviceinfo $2 -u "$1" -k ProductType 2>/dev/null)"
  ios="$(timeout 10 ideviceinfo $2 -u "$1" -k ProductVersion 2>/dev/null)"
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$3" "$ok" "${name:--}" "${model:--}" "${ios:--}"
}

seen=" "
for u in $(timeout 10 idevice_id -l 2>/dev/null | cut -d' ' -f1); do
  emit "$u" "" usb
  seen="$seen$u "
done
for u in $(timeout 10 idevice_id -n 2>/dev/null | cut -d' ' -f1); do
  case "$seen" in *" $u "*) continue ;; esac
  emit "$u" "-n" wifi
done
exit 0
