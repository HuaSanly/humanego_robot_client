#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly TOKEN_FILE="$SCRIPT_DIR/.control-token"
readonly PORT="${HUMANEGO_PORT:-8876}"
readonly BIND="127.0.0.1"

if [[ "$(uname -s)" != "Linux" ]] || ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 控制台需要 Linux 和 Python 3。" >&2
  exit 2
fi

if [[ -n "${TELEOP_TOKEN:-}" ]]; then
  TOKEN="$TELEOP_TOKEN"
elif [[ -s "$TOKEN_FILE" ]]; then
  IFS= read -r TOKEN < "$TOKEN_FILE"
else
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  (umask 077; printf '%s\n' "$TOKEN" > "$TOKEN_FILE")
fi
chmod 600 "$TOKEN_FILE" 2>/dev/null || true

if [[ "${EUID}" -eq 0 ]]; then
  OPERATOR="${TELEOP_OPERATOR_USER:-${SUDO_USER:-}}"
  if [[ -z "$OPERATOR" || "$OPERATOR" == "root" ]]; then
    echo "[错误] 以 root 启动时请设置 TELEOP_OPERATOR_USER。" >&2
    exit 2
  fi
  OPERATOR_HOME="${TELEOP_OPERATOR_HOME:-$(getent passwd "$OPERATOR" | cut -d: -f6)}"
else
  OPERATOR="${USER}"
  OPERATOR_HOME="${HOME}"
fi

URL="http://127.0.0.1:${PORT}/?token=${TOKEN}"
echo "HumanEgo 控制台：${URL}"
echo "操作用户：${OPERATOR}"

if [[ "${TELEOP_NO_BROWSER:-0}" != "1" && -n "${DISPLAY:-}" ]] && command -v xdg-open >/dev/null 2>&1; then
  (sleep 2; xdg-open "$URL" >/dev/null 2>&1 || true) &
fi

if [[ "${EUID}" -ne 0 ]]; then
  if ! sudo -v; then
    echo "[错误] sudo 验证失败，控制台未启动。" >&2
    exit 1
  fi
  exec sudo -n /usr/bin/env \
    "TELEOP_OPERATOR_USER=${OPERATOR}" \
    "TELEOP_OPERATOR_HOME=${OPERATOR_HOME}" \
    "TELEOP_TOKEN=${TOKEN}" \
    "TELEOP_BIND=${BIND}" \
    "HUMANEGO_PORT=${PORT}" \
    DISPLAY="${DISPLAY:-}" \
    XAUTHORITY="${XAUTHORITY:-}" \
    DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-}" \
    /usr/bin/python3 "$SCRIPT_DIR/server.py" "$@"
fi

export TELEOP_OPERATOR_USER="$OPERATOR"
export TELEOP_OPERATOR_HOME="$OPERATOR_HOME"
export TELEOP_TOKEN="$TOKEN"
export TELEOP_BIND="$BIND"
export HUMANEGO_PORT="$PORT"
exec /usr/bin/python3 "$SCRIPT_DIR/server.py" "$@"
