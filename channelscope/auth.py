"""Sign in to your YouTube channel with Google OAuth (one browser click, then cached)."""
from __future__ import annotations

import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "channelscope"

READ_SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
# captions.download only works with this broader scope, so it's asked for only with --captions-api.
CAPTIONS_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"

SETUP_HELP = """\
No Google OAuth client found at {path}.

One-time setup (about 5 minutes):
  1. Go to https://console.cloud.google.com/ and create a project (any name).
  2. APIs & Services > Library: enable "YouTube Data API v3",
     "YouTube Analytics API" and "YouTube Reporting API".
  3. APIs & Services > OAuth consent screen: choose External, fill in the app
     name and your email, and add your own Google account as a test user.
  4. APIs & Services > Credentials > Create credentials > OAuth client ID >
     Application type "Desktop app". Download the JSON.
  5. Save it as {path}
     (or pass --client-secret path/to/file.json).

Then run channelscope again. A browser window opens once to sign in. If your
channel is a Brand Account, pick the channel (not your personal account) there."""


class SetupError(RuntimeError):
    pass


def credentials(client_secret: Path, token_path: Path, scopes: list[str]):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path))
        if not set(scopes) <= set(creds.scopes or []):
            creds = None  # new scope needed (e.g. first --captions-api run): sign in again
    if creds and not creds.valid and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            creds = None
    if not creds or not creds.valid:
        if not client_secret.exists():
            raise SetupError(SETUP_HELP.format(path=client_secret))
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), scopes)
        creds = flow.run_local_server(port=0, prompt="consent")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    os.chmod(token_path, 0o600)
    return creds


def services(creds):
    """-> (YouTube Data v3, YouTube Analytics v2, YouTube Reporting v1)."""
    from googleapiclient.discovery import build

    return (
        build("youtube", "v3", credentials=creds, cache_discovery=False),
        build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False),
        build("youtubereporting", "v1", credentials=creds, cache_discovery=False),
    )
