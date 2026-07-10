#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-${ROOT}/dist/release}"
BUILD_DIR="${PHAROS_BUILD_DIR:-${ROOT}/build/pyinstaller}"
export PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR:-${BUILD_DIR}/cache}"
case "$(uname -s)" in Darwin) platform=darwin ;; Linux) platform=linux ;; *) echo "Unsupported OS: $(uname -s)" >&2; exit 2 ;; esac
case "$(uname -m)" in arm64|aarch64) arch=arm64 ;; x86_64|amd64) arch=x86_64 ;; *) echo "Unsupported architecture: $(uname -m)" >&2; exit 2 ;; esac
mkdir -p "${OUTPUT_DIR}" "${BUILD_DIR}/dist" "${BUILD_DIR}/work" "${BUILD_DIR}/spec" "${PYINSTALLER_CONFIG_DIR}"
uv run --group freeze pyinstaller --noconfirm --onefile --clean \
  --paths "${ROOT}/src" --collect-submodules pharos --collect-submodules gnomon \
  --collect-submodules orrery_heartbeat --name pharos --distpath "${BUILD_DIR}/dist" \
  --workpath "${BUILD_DIR}/work/pharos" --specpath "${BUILD_DIR}/spec" \
  "${ROOT}/scripts/pharos_entry.py"
install -m 0755 "${BUILD_DIR}/dist/pharos" "${OUTPUT_DIR}/pharos-${platform}-${arch}"
if [[ "${SKIP_SMOKE:-0}" != "1" ]]; then
  smoke_root="$(mktemp -d)"
  CI=1 ORRERY_NO_UPDATE_CHECK=1 XDG_DATA_HOME="${smoke_root}/data" XDG_CACHE_HOME="${smoke_root}/cache" \
    "${OUTPUT_DIR}/pharos-${platform}-${arch}" --help >/dev/null
  rm -rf "${smoke_root}"
fi
