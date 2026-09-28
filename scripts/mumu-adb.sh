#!/usr/bin/env bash
# Auto-detect the running MuMu emulator's adb port and connect to it.
# macOS + MuMu Player. Prints an `export MANO_DEVICE_SERIAL=...` line on stdout;
# status goes to stderr, so you can do:  eval "$(bash scripts/mumu-adb.sh)"
set -uo pipefail

adb kill-server  >/dev/null 2>&1 || true
adb start-server >/dev/null 2>&1 || true

# Ports the MuMuEmulator process is listening on (the adb port is one of them).
ports=$(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null \
  | awk '/MuMuEmula/ {print $9}' \
  | sed -E 's/.*:([0-9]+)$/\1/' \
  | sort -u)

if [ -z "${ports}" ]; then
  echo "未发现 MuMu 监听端口——模拟器是否已启动到 Android 桌面？" >&2
  exit 1
fi

for port in ${ports}; do
  serial="127.0.0.1:${port}"
  adb connect "${serial}" >/dev/null 2>&1 || true
  state=$(adb -s "${serial}" get-state 2>/dev/null || true)
  if [ "${state}" != "device" ]; then      # offline/unauthorized: retry once
    sleep 1
    state=$(adb -s "${serial}" get-state 2>/dev/null || true)
  fi
  if [ "${state}" = "device" ]; then
    echo "CONNECTED ${serial}" >&2
    echo "export MANO_DEVICE_SERIAL=${serial}"
    exit 0
  fi
  adb disconnect "${serial}" >/dev/null 2>&1 || true
done

echo "试过端口 [${ports}] 都没就绪；等 MuMu 完全启动后重试。" >&2
exit 1
