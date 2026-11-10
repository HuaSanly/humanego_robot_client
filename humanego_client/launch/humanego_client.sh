#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if ! command -v conda >/dev/null 2>&1; then
  for conda_sh in \
    "${HOME}/miniconda3/etc/profile.d/conda.sh" \
    "${HOME}/anaconda3/etc/profile.d/conda.sh" \
    "${HOME}/miniforge3/etc/profile.d/conda.sh" \
    "${HOME}/mambaforge/etc/profile.d/conda.sh" \
    "${TELEOP_HOME:-${HOME}}/miniconda3/etc/profile.d/conda.sh" \
    "${TELEOP_HOME:-${HOME}}/anaconda3/etc/profile.d/conda.sh" \
    "${TELEOP_HOME:-${HOME}}/miniforge3/etc/profile.d/conda.sh" \
    "${TELEOP_HOME:-${HOME}}/mambaforge/etc/profile.d/conda.sh" \
    "/opt/conda/etc/profile.d/conda.sh"; do
    if [[ -r "${conda_sh}" ]]; then
      # shellcheck disable=SC1090
      source "${conda_sh}"
      break
    fi
  done
fi

if ! command -v conda >/dev/null 2>&1; then
  echo "[ERROR] Conda was not found in PATH or common installation directories." >&2
  echo "        Set up Conda for ${USER:-the operator}, or update this launcher with its conda.sh path." >&2
  exit 127
fi

CONDA_BASE="$(conda info --base)"
if [[ ! -r "${CONDA_BASE}/etc/profile.d/conda.sh" ]]; then
  echo "[ERROR] Conda activation script is missing: ${CONDA_BASE}/etc/profile.d/conda.sh" >&2
  exit 127
fi
source "${CONDA_BASE}/etc/profile.d/conda.sh"
conda activate humanego
export PYTHONPATH="$(cd "$(dirname "$0")/../src" && pwd)"
exec python -m humanego_client.main --config "${ROOT}/config/client.yaml" "$@"
