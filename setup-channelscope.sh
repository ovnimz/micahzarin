#!/bin/bash
# One-shot setup for channelscope. Run it, follow the prompts, and it ends by
# writing your first report. Safe to run again: finished steps are skipped.
#
#   bash setup-channelscope.sh           # set up (or finish setting up) and run
#   bash setup-channelscope.sh --reset   # forget the Google project + sign-in and start over
#
# Google doesn't allow the OAuth client (the "sign-in key" channelscope uses)
# to be created by a script, so for that part this opens the right page and
# tells you exactly what to click. Everything else is automatic.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$HOME/.config/channelscope"
SECRET="$CONF/client_secret.json"
PROJECT_FILE="$CONF/project"
STUDIO="$CONF/studio.csv"
REPORTS="$HOME/channelscope-reports"
DOWNLOADS="${CHANNELSCOPE_DOWNLOADS:-$HOME/Downloads}"
RC="$HOME/.zshrc"
[ -n "${BASH_VERSION:-}" ] && [ "$(basename "${SHELL:-}")" = "bash" ] && RC="$HOME/.bash_profile"
APIS="youtube.googleapis.com youtubeanalytics.googleapis.com youtubereporting.googleapis.com"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ask() { local r; read -r -p "$1 " r </dev/tty || true; printf '%s' "$r"; }
pause() { read -r -p "${1:-Press Enter when that is done...} " _ </dev/tty || true; }
yes_no() { case "$(ask "$1 [y/N]")" in [yY]*) return 0 ;; *) return 1 ;; esac; }
open_url() {
  echo "  Opening: $1"
  if [ "$(uname)" = Darwin ]; then open "$1" 2>/dev/null || true
  elif command -v xdg-open >/dev/null; then xdg-open "$1" >/dev/null 2>&1 || true
  fi
  echo "  (if no browser window appeared, copy that link into your browser)"
}

valid_secret() {
  python3 - "$1" <<'EOF' 2>/dev/null
import json, sys
d = json.load(open(sys.argv[1]))
sys.exit(0 if d.get("installed", {}).get("client_id") and d["installed"].get("client_secret") else 1)
EOF
}
secret_kind() {
  python3 -c 'import json,sys; print(next(iter(json.load(open(sys.argv[1])))))' "$1" 2>/dev/null || echo "?"
}

# Wait until a matching new file shows up in Downloads, or the user types/drags a path.
# $1 = find -name pattern(s) separated by |, $2 = marker file (only files newer than it count)
wait_for_download() {
  local patterns="$1" marker="$2" found="" line="" p
  echo "  Waiting for the download to show up in $DOWNLOADS ..."
  echo "  (or drag the downloaded file into this window and press Enter)"
  while :; do
    IFS='|' read -r -a pats <<< "$patterns"
    for p in "${pats[@]}"; do
      found="$(find "$DOWNLOADS" -maxdepth 1 -name "$p" -newer "$marker" \
        ! -name '*.crdownload' ! -name '*.download' ! -name '*.part' 2>/dev/null | head -1)"
      [ -n "$found" ] && { WAITED_FILE="$found"; return 0; }
    done
    line=""
    if read -r -t 2 line </dev/tty 2>/dev/null && [ -n "$line" ]; then
      if [ "$line" = "manual" ] && [ -n "${ALLOW_MANUAL:-}" ]; then WAITED_FILE="manual"; return 0; fi
      # Dragged paths arrive quoted or with backslash-escaped spaces.
      line="${line%"${line##*[![:space:]]}"}"
      line="${line#\'}"; line="${line%\'}"; line="${line#\"}"; line="${line%\"}"
      line="$(printf '%s' "$line" | sed 's/\\\(.\)/\1/g')"
      if [ -e "$line" ]; then WAITED_FILE="$line"; return 0; fi
      echo "  Can't find \"$line\"; keep waiting or try again."
    fi
  done
}

if [ "${1:-}" = "--reset" ]; then
  rm -f "$SECRET" "$PROJECT_FILE" "$CONF"/token-*.json
  echo "Forgot the Google project and sign-in. Run bash setup-channelscope.sh again to start over."
  exit 0
fi
mkdir -p "$CONF" "$REPORTS"
chmod 700 "$CONF"
HAVE_SECRET=""
[ -f "$SECRET" ] && valid_secret "$SECRET" && HAVE_SECRET=1

# ---------------------------------------------------------------- 1. install
step "1/7  Installing channelscope"
if [ -z "${CHANNELSCOPE_SKIP_INSTALL:-}" ]; then
  py_ok() { command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; }
  if ! py_ok; then
    if command -v brew >/dev/null; then
      echo "Installing Python with Homebrew..."
      brew install python
    else
      bold "You need Python 3.10 or newer. Download the macOS installer, run it, then come back here."
      open_url "https://www.python.org/downloads/"
      until py_ok; do pause "Press Enter once Python is installed..."; hash -r; done
    fi
  fi
  bash "$DIR/install.sh" >/dev/null
  echo "  installed"
fi
CHANNELSCOPE="${CHANNELSCOPE_BIN:-$DIR/.venv/bin/channelscope}"

# ---------------------------------------------------------------- 2. Claude key
step "2/7  Claude API key"
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "  found in this Terminal"
elif grep -q "ANTHROPIC_API_KEY" "$RC" 2>/dev/null; then
  # shellcheck disable=SC1090
  eval "$(grep -E '^export ANTHROPIC_API_KEY=' "$RC" | tail -1)"
  echo "  found in $RC"
else
  echo "channelscope uses Claude to read your thumbnails, transcripts and comments."
  echo "Create a key (Create Key > copy it), then paste it here."
  open_url "https://console.anthropic.com/settings/keys"
  while :; do
    read -r -s -p "Paste your key (it won't show as you paste) and press Enter: " key </dev/tty || true
    echo
    key="$(printf '%s' "$key" | tr -d '[:space:]')"
    case "$key" in sk-ant-*) break ;; *) echo "  That doesn't look like a Claude key (they start with sk-ant-). Try again." ;; esac
  done
  printf "\nexport ANTHROPIC_API_KEY='%s'\n" "$key" >> "$RC"
  export ANTHROPIC_API_KEY="$key"
  echo "  saved to $RC"
fi

# ---------------------------------------------------------------- 3. gcloud
step "3/7  Google Cloud tools"
find_gcloud() {
  for g in "$(command -v gcloud 2>/dev/null || true)" "$HOME/google-cloud-sdk/bin/gcloud" /opt/homebrew/bin/gcloud /usr/local/bin/gcloud \
           /opt/homebrew/share/google-cloud-sdk/bin/gcloud /usr/local/share/google-cloud-sdk/bin/gcloud; do
    [ -n "$g" ] && [ -x "$g" ] && { echo "$g"; return 0; }
  done
  return 1
}
if [ -n "$HAVE_SECRET" ]; then
  echo "  not needed: your Google sign-in key is already set up"
else
  if ! GCLOUD="$(find_gcloud)"; then
    if command -v brew >/dev/null; then
      echo "Installing the Google Cloud CLI with Homebrew (takes a minute)..."
      brew install --cask gcloud-cli
    else
      if [ "$(uname)" = Darwin ] && ! xcode-select -p >/dev/null 2>&1; then
        bold "macOS will ask to install its Command Line Tools. Click Install and wait for it to finish."
        xcode-select --install 2>/dev/null || true
        until xcode-select -p >/dev/null 2>&1; do sleep 5; done
      fi
      echo "Installing the Google Cloud CLI into ~/google-cloud-sdk (takes a minute; it may ask for your Mac password)..."
      tmp="$(mktemp)"
      curl -fsSL https://sdk.cloud.google.com -o "$tmp"
      bash "$tmp" --disable-prompts --install-dir="$HOME" >/dev/null
      rm -f "$tmp"
    fi
    GCLOUD="$(find_gcloud)" || { echo "Couldn't find gcloud after installing it. Open a new Terminal window and run this again."; exit 1; }
  fi
  echo "  using $GCLOUD"
fi

# ---------------------------------------------------------------- 4. project + APIs
step "4/7  Google Cloud project"
if [ -n "$HAVE_SECRET" ]; then
  echo "  already set up"
else
  ACCOUNT="$("$GCLOUD" auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1 || true)"
  if [ -n "$ACCOUNT" ] && ! yes_no "Use the Google account $ACCOUNT (it should be the one you use for YouTube)?"; then
    ACCOUNT=""
  fi
  if [ -z "$ACCOUNT" ]; then
    bold "A browser window will open: sign in with the Google account you use for YouTube, then click Allow."
    pause "Press Enter to open it..."
    "$GCLOUD" auth login --brief --quiet
    ACCOUNT="$("$GCLOUD" auth list --filter=status:ACTIVE --format='value(account)' | head -1)"
  fi
  echo "  signed in as $ACCOUNT"

  PROJECT="$(cat "$PROJECT_FILE" 2>/dev/null || true)"
  if [ -n "$PROJECT" ] && "$GCLOUD" projects describe "$PROJECT" --format='value(projectId)' >/dev/null 2>&1; then
    echo "  reusing project $PROJECT"
  else
    PROJECT="channelscope-$(od -An -N4 -tx4 /dev/urandom | tr -d ' \n')"
    until "$GCLOUD" projects create "$PROJECT" --name="channelscope" --quiet >/dev/null 2>&1; do
      bold "Google wouldn't create the project yet. This usually means you haven't accepted Google Cloud's terms."
      echo "In the page that opens: pick your country, tick the box to agree, click Agree and continue. You don't need a free trial or billing."
      open_url "https://console.cloud.google.com/"
      pause "Press Enter to try again..."
    done
    echo "$PROJECT" > "$PROJECT_FILE"
    echo "  created project $PROJECT"
  fi
  "$GCLOUD" config set project "$PROJECT" --quiet >/dev/null 2>&1 || true

  echo "  turning on the YouTube APIs..."
  for attempt in 1 2 3 4 5 6; do
    # shellcheck disable=SC2086
    if "$GCLOUD" services enable $APIS --project="$PROJECT" --quiet >/dev/null 2>&1; then break; fi
    [ "$attempt" = 6 ] && { echo "Couldn't turn on the YouTube APIs. Run this script again in a minute."; exit 1; }
    sleep 15  # brand-new projects take a moment before APIs can be enabled
  done
  echo "  YouTube Data, Analytics and Reporting APIs are on"
fi

# ---------------------------------------------------------------- 5. OAuth client (the clicky part)
step "5/7  Your sign-in key (3 short pages in the browser)"
if [ -n "$HAVE_SECRET" ]; then
  echo "  already set up"
else
  bold "Page 1 of 3: name the app"
  cat <<EOF
  1. Click "Get started"
  2. App name: channelscope    User support email: pick $ACCOUNT    then Next
  3. Audience: External    then Next
  4. Contact information: type $ACCOUNT    then Next
  5. Tick "I agree to the Google API Services: User Data Policy", click Continue, then Create
  (If it says the app is already set up, there's nothing to do on this page.)
EOF
  open_url "https://console.cloud.google.com/auth/overview?project=$PROJECT"
  pause

  bold "Page 2 of 3: let yourself in"
  cat <<EOF
  1. Under "Test users", click "+ Add users", type $ACCOUNT, click Save
  2. Under "Publishing status", click "Publish app", then Confirm
     (so you don't have to sign in again every week. Only you will use it,
     so Google doesn't need to review it.)
EOF
  open_url "https://console.cloud.google.com/auth/audience?project=$PROJECT"
  pause

  bold "Page 3 of 3: make the key"
  cat <<EOF
  1. Click "+ Create client"
  2. Application type: Desktop app    Name: channelscope    then Create
  3. In the box that pops up, click "Download JSON" (do it now: Google only
     lets you download it this once)
EOF
  echo "  No Download JSON button? Type manual and press Enter to paste the Client ID and secret instead."
  marker="$(mktemp)"
  sleep 1
  open_url "https://console.cloud.google.com/auth/clients?project=$PROJECT"
  ALLOW_MANUAL=1
  while :; do
    wait_for_download "client_secret*.json|*.apps.googleusercontent.com.json" "$marker"
    if [ "$WAITED_FILE" = "manual" ]; then
      cid="$(ask "Client ID (ends in .apps.googleusercontent.com):" | tr -d '[:space:]')"
      csec="$(ask "Client secret (starts with GOCSPX-):" | tr -d '[:space:]')"
      WAITED_FILE="$(mktemp)"
      python3 - "$cid" "$csec" "$WAITED_FILE" <<'PY'
import json, sys
cid, sec, out = sys.argv[1:]
json.dump({"installed": {
    "client_id": cid, "client_secret": sec, "project_id": "",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs", "redirect_uris": ["http://localhost"],
}}, open(out, "w"))
PY
      case "$cid" in *.apps.googleusercontent.com) [ -n "$csec" ] && break ;; esac
      echo "  That Client ID doesn't look right; try again."
      continue
    fi
    if valid_secret "$WAITED_FILE"; then break; fi
    if [ "$(secret_kind "$WAITED_FILE")" = "web" ]; then
      echo "  That key is for a \"Web application\". Create another client and choose \"Desktop app\" as the type."
    else
      echo "  $WAITED_FILE isn't a Desktop app key. Try the download again."
    fi
    touch "$marker"
  done
  ALLOW_MANUAL=""
  mv "$WAITED_FILE" "$SECRET"
  chmod 600 "$SECRET"
  rm -f "$marker"
  echo "  saved your key to $SECRET"
fi

# ---------------------------------------------------------------- 6. CTR (optional)
step "6/7  Click-through rate (optional, 2 minutes)"
echo "YouTube only gives impressions and CTR through a Studio export. Without it, CTR fills in"
echo "automatically over the coming weeks instead."
if [ -f "$STUDIO" ]; then
  echo "  You already have one from $(date -r "$STUDIO" '+%b %d' 2>/dev/null || echo earlier)."
fi
if yes_no "Export it from YouTube Studio now?"; then
  cat <<'EOF'
  1. In YouTube Studio click Analytics (left), then "Advanced mode" (top right)
  2. Set the date range (top right) to "Lifetime"
  3. Make sure "Impressions" and "Impressions click-through rate" are columns.
     If not, click the + / metrics button above the table and tick them.
  4. Click the download arrow (top right) > "Comma-separated values (.csv)"
EOF
  marker="$(mktemp)"
  sleep 1
  open_url "https://studio.youtube.com/"
  wait_for_download "*.zip|Table data*.csv|Content *" "$marker"
  rm -f "$marker"
  csv=""
  if [ -d "$WAITED_FILE" ]; then
    csv="$(find "$WAITED_FILE" -name 'Table data*.csv' | head -1)"
  elif [ "${WAITED_FILE##*.}" = "zip" ]; then
    tmpd="$(mktemp -d)"
    unzip -q -o "$WAITED_FILE" -d "$tmpd"
    csv="$(find "$tmpd" -name 'Table data*.csv' | head -1)"
  else
    csv="$WAITED_FILE"
  fi
  if [ -n "$csv" ] && grep -qi "click-through" "$csv"; then
    cp "$csv" "$STUDIO"
    echo "  saved; channelscope will use it automatically"
  else
    echo "  That export doesn't have the Impressions click-through rate column. Skipping CTR for now;"
    echo "  you can rerun this script later to try again."
  fi
fi

# ---------------------------------------------------------------- 7. run it
step "7/7  Your first report"
NEXT="$(ask "Your next episode, to get titles and thumbnails for it (e.g. \"Jane Doe, ex-FBI negotiator\"), or Enter to skip:")"
if [ ! -f "$CONF/token-default.json" ]; then
  bold "One more browser window: sign in to YouTube"
  cat <<'EOF'
  - Pick the account (or the Brand Account channel) your videos are on
  - "Google hasn't verified this app": click Continue (or Advanced > Go to channelscope).
    It's your own app, so this is expected.
  - Tick every box and click Continue
EOF
  pause "Press Enter to open it..."
fi
cd "$REPORTS"
args=()
[ -n "$NEXT" ] && args+=(--next "$NEXT")
"$CHANNELSCOPE" ${args[@]+"${args[@]}"}

report="$(ls -t "$REPORTS"/channelscope-*.html 2>/dev/null | head -1 || true)"
bold "Done."
echo "Your report is in $REPORTS"
if [ -n "$report" ]; then
  echo "Opening $report"
  open_url "file://$report"
fi
cat <<'EOF'

From now on, just open a new Terminal window and type:
  channelscope                              # refresh the report
  channelscope --next "guest and topic"     # plus titles/thumbnails for your next episode
EOF
