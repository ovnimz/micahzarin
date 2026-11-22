#!/bin/bash
# Download the latest guestprep and reinstall it. Uses your GitHub login (via the
# `gh` tool), so it works when the repository is private.
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="ovnimz/micahzarin"

if ! command -v gh >/dev/null; then
  echo "guestprep-update needs GitHub's command-line tool. Install it and log in once:" >&2
  echo "  brew install gh" >&2
  echo "  gh auth login" >&2
  exit 1
fi
if ! TOKEN="$(gh auth token 2>/dev/null)" || [ -z "$TOKEN" ]; then
  echo "You're not logged in to GitHub. Run this once, then try again:" >&2
  echo "  gh auth login" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# The API tarball is the default branch; the token lets it read a private repository.
if ! curl -fsSL -H "Authorization: Bearer $TOKEN" "https://api.github.com/repos/$REPO/tarball" -o "$TMP/guestprep.tar.gz"; then
  echo "Couldn't download $REPO. Check that 'gh auth status' shows the account that owns it." >&2
  exit 1
fi
tar -xzf "$TMP/guestprep.tar.gz" --strip-components=1 -C "$DIR"
"$DIR/.venv/bin/pip" install -q -e "$DIR"
echo "guestprep updated"
