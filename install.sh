#!/bin/bash
# One-time setup: installs guestprep and adds `guestprep` + `guestprep-update`
# to every new Terminal window. Safe to run again.
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RC="$HOME/.zshrc"
[ -n "$BASH_VERSION" ] && [ "$(basename "$SHELL")" = "bash" ] && RC="$HOME/.bash_profile"

PY="$(command -v python3 || true)"
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  echo "guestprep needs Python 3.10 or newer: https://www.python.org/downloads/" >&2
  exit 1
fi

echo "Installing into $DIR/.venv ..."
[ -d "$DIR/.venv" ] || "$PY" -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -e "$DIR"

# Replace any previous guestprep block in the shell profile with a fresh one.
touch "$RC"
awk '/# >>> guestprep >>>/{skip=1} !skip{print} /# <<< guestprep <<</{skip=0}' "$RC" > "$RC.guestprep.tmp"
mv "$RC.guestprep.tmp" "$RC"
cat >> "$RC" <<EOF
# >>> guestprep >>>
alias guestprep="$DIR/.venv/bin/guestprep"
alias guestprep-update="bash '$DIR/update.sh'"
# <<< guestprep <<<
EOF

echo
echo "Done. Open a new Terminal window, then from any folder:"
echo "  guestprep \"Guest Name\" --web"
echo "  guestprep-update        # get the latest version"
if ! command -v gh >/dev/null || ! gh auth token >/dev/null 2>&1; then
  echo
  echo "guestprep-update downloads from GitHub with your login (the repository is private)."
  echo "Set that up once with:"
  echo "  brew install gh"
  echo "  gh auth login"
fi
if ! grep -q "ANTHROPIC_API_KEY" "$RC"; then
  echo
  echo "You still need a Claude API key. Add it with:"
  echo "  echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> $RC"
fi
