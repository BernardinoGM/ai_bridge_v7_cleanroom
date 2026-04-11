#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${HOME}/.aibridge"
PIP_BIN="${INSTALL_DIR}/bin/pip"
PYTHON_BIN="${INSTALL_DIR}/bin/python"
PACKAGE_REF="git+https://github.com/BernardinoGM/ai_bridge_v7_cleanroom.git@main"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required to install aibridge." >&2
  exit 1
fi

if [ ! -x "${PYTHON_BIN}" ]; then
  python3 -m venv "${INSTALL_DIR}" >/dev/null 2>&1
fi

"${PIP_BIN}" install --disable-pip-version-check --quiet --upgrade "${PACKAGE_REF}" >/dev/null

printf 'Installed: %s\n' "${INSTALL_DIR}/bin/aibridge"
