#!/bin/bash
# One-time setup: installs guestprep and adds `guestprep`, `channelscope` and
# `guestprep-update` to every new Terminal window. Safe to run again.
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RC="$HOME/.zshrc"
[ -n "$BASH_VERSION" ] && [ "$(basename "$SHELL")" = "bash" ] && RC="$HOME/.bash_profile"

PY="$(command -v python3 || true)"
# Apple's /usr/bin/python3 is a stub (and only 3.9) until the developer tools are installed.
if [ "$PY" = /usr/bin/python3 ] && [ "$(uname)" = Darwin ] && ! xcode-select -p >/dev/null 2>&1; then PY=""; fi
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
cat "$RC.guestprep.tmp" > "$RC"  # keeps a symlinked profile a symlink
rm -f "$RC.guestprep.tmp"
cat >> "$RC" <<EOF
# >>> guestprep >>>
alias guestprep="$DIR/.venv/bin/guestprep"
alias channelscope="$DIR/.venv/bin/channelscope"
alias channelscope-setup="bash '$DIR/setup-channelscope.sh'"
alias guestprep-update="curl -fsSL https://github.com/ovnimz/micahzarin/archive/HEAD.tar.gz | tar -xz --strip-components=1 -C '$DIR' && '$DIR/.venv/bin/pip' install -q -e '$DIR' && echo 'guestprep updated'"
# <<< guestprep <<<
EOF

echo
echo "Done. Open a new Terminal window, then from any folder:"
echo "  guestprep \"Guest Name\" --web"
echo "  channelscope-setup      # first time only: connects your YouTube channel, then runs channelscope"
echo "  guestprep-update        # get the latest version"
if ! grep -q "ANTHROPIC_API_KEY" "$RC"; then
  echo
  echo "You still need a Claude API key. Add it with:"
  echo "  echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> $RC"
fi
