#!/usr/bin/env bash
set -euo pipefail

if [[ -f /opt/ros/noetic/setup.bash ]]; then
  # shellcheck disable=SC1091
  source /opt/ros/noetic/setup.bash
else
  echo "ROS Noetic setup file not found: /opt/ros/noetic/setup.bash" >&2
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${script_dir}/move_head_down_10deg.py" "$@"
