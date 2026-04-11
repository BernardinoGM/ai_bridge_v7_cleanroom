#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${HOME}/.aibridge"
LOCAL_BIN_DIR="${HOME}/.local/bin"
PACKAGE_REF="git+https://github.com/BernardinoGM/ai_bridge_v7_cleanroom.git@main"
PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'

ensure_path_line() {
  local file="$1"
  mkdir -p "$(dirname "$file")"
  touch "$file"
  if ! grep -Fqs "$PATH_LINE" "$file"; then
    printf '\n%s\n' "$PATH_LINE" >> "$file"
  fi
}

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required to install aibridge." >&2
  exit 1
fi

python3 -m venv "$INSTALL_DIR" >/dev/null 2>&1
"$INSTALL_DIR/bin/pip" install --disable-pip-version-check --quiet --upgrade --force-reinstall "$PACKAGE_REF"

mkdir -p "$LOCAL_BIN_DIR"
ln -sf "$INSTALL_DIR/bin/aibridge" "$LOCAL_BIN_DIR/aibridge"

ensure_path_line "$HOME/.profile"
case "$(basename "${SHELL:-}")" in
  zsh)
    ensure_path_line "$HOME/.zshrc"
    ;;
  bash)
    if [ -f "$HOME/.bash_profile" ]; then
      ensure_path_line "$HOME/.bash_profile"
    else
      ensure_path_line "$HOME/.bashrc"
    fi
    ;;
esac

printf 'Installed aibridge.\n'
printf 'Next:\n'
printf '  export AB_API_KEY="ab_live_..."\n'
printf '  aibridge\n'
