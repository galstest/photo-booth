#!/usr/bin/env python3
"""
Interactive setup wizard for the Google Photos monitor.
Run once before starting monitor.py:

    python setup.py
"""

import json
import sys
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

CONFIG_FILE = Path("config.json")
TOKEN_FILE = Path("token.json")
SCOPES = ["https://www.googleapis.com/auth/photoslibrary"]
BASE_URL = "https://photoslibrary.googleapis.com/v1"

# ANSI helpers
BOLD = "\033[1m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"


def pr(msg: str = "") -> None:
    print(msg)


def header(msg: str) -> None:
    print(f"\n{BOLD}{CYAN}{msg}{RESET}")


def success(msg: str) -> None:
    print(f"{GREEN}✓ {msg}{RESET}")


def warn(msg: str) -> None:
    print(f"{YELLOW}⚠ {msg}{RESET}")


def error(msg: str) -> None:
    print(f"{RED}✗ {msg}{RESET}")


def prompt(msg: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    val = input(f"{BOLD}{msg}{hint}:{RESET} ").strip()
    return val or default


def prompt_int(msg: str, default: int) -> int:
    while True:
        raw = prompt(msg, str(default))
        try:
            return int(raw)
        except ValueError:
            error("Please enter a whole number.")


# ---------------------------------------------------------------------------
# Step 1 — credentials.json
# ---------------------------------------------------------------------------
def step_credentials() -> Path:
    header("Step 1 of 4 — Google Cloud credentials")
    pr(
        "You need an OAuth 2.0 client secrets file from Google Cloud Console.\n"
        "If you haven't done this yet:\n"
        "  1. Go to https://console.cloud.google.com/\n"
        "  2. Enable the Google Photos Library API\n"
        "  3. APIs & Services → Credentials → Create Credentials → OAuth client ID\n"
        "  4. Application type: Desktop app\n"
        "  5. Download the JSON file and put it anywhere accessible\n"
    )

    while True:
        path_str = prompt("Path to credentials JSON file", "credentials.json")
        path = Path(path_str).expanduser()
        if path.exists():
            try:
                data = json.loads(path.read_text())
                if "installed" not in data and "web" not in data:
                    error("That doesn't look like a valid OAuth client secrets file.")
                    continue
                success(f"Found credentials file: {path}")
                return path
            except json.JSONDecodeError:
                error("File is not valid JSON.")
        else:
            error(f"File not found: {path}")


# ---------------------------------------------------------------------------
# Step 2 — OAuth authentication
# ---------------------------------------------------------------------------
def step_auth(credentials_file: Path) -> tuple[Credentials, requests.Session]:
    header("Step 2 of 4 — Google account authentication")
    pr("A browser window will open for you to sign in and grant access.")
    pr("(If the browser doesn't open, copy the URL printed below.)\n")

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            pr("Refreshing existing token...")
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(creds.to_json())

    success("Authenticated successfully.")

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {creds.token}"})
    return creds, session


# ---------------------------------------------------------------------------
# Step 3 — pick album
# ---------------------------------------------------------------------------
def fetch_all_albums(session: requests.Session) -> list[dict]:
    albums = []
    page_token = None
    while True:
        params = {"pageSize": 50}
        if page_token:
            params["pageToken"] = page_token
        resp = session.get(f"{BASE_URL}/albums", params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        albums.extend(data.get("albums", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return albums


def step_album(session: requests.Session) -> str:
    header("Step 3 of 4 — target album")
    pr("Fetching your Google Photos albums...")

    try:
        albums = fetch_all_albums(session)
    except requests.exceptions.HTTPError as exc:
        error(f"Could not fetch albums: {exc}")
        sys.exit(1)

    if not albums:
        warn("No albums found in your Google Photos library.")
        pr("You can enter an album ID manually (create one in Google Photos first).")
        return prompt("Album ID")

    pr(f"\nFound {len(albums)} album(s):\n")
    for i, album in enumerate(albums, 1):
        title = album.get("title", "(untitled)")
        count = album.get("mediaItemsCount", "?")
        print(f"  {BOLD}{i:>3}.{RESET} {title}  ({count} items)")

    pr()
    while True:
        raw = prompt("Enter the number of the target album (or paste an album ID directly)")
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(albums):
                chosen = albums[idx]
                success(f"Selected: {chosen.get('title')}")
                return chosen["id"]
            else:
                error(f"Please enter a number between 1 and {len(albums)}.")
        elif raw:
            # Assume it's a raw album ID
            success(f"Using album ID: {raw}")
            return raw
        else:
            error("Please make a selection.")


# ---------------------------------------------------------------------------
# Step 4 — device model
# ---------------------------------------------------------------------------
def fetch_recent_models(session: requests.Session, limit: int = 200) -> list[str]:
    """Return a deduplicated list of cameraModel values from recent photos."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    body = {
        "pageSize": limit,
        "filters": {
            "dateFilter": {
                "ranges": [
                    {
                        "startDate": {"year": now.year - 1, "month": now.month, "day": now.day},
                        "endDate":   {"year": now.year,     "month": now.month, "day": now.day},
                    }
                ]
            },
            "mediaTypeFilter": {"mediaTypes": ["PHOTO"]},
        },
    }
    resp = session.post(f"{BASE_URL}/mediaItems:search", json=body, timeout=30)
    resp.raise_for_status()
    items = resp.json().get("mediaItems", [])
    seen = {}
    for item in items:
        model = item.get("mediaMetadata", {}).get("photo", {}).get("cameraModel", "")
        if model:
            seen[model] = seen.get(model, 0) + 1
    # Sort by frequency
    return sorted(seen, key=lambda m: seen[m], reverse=True)


def step_device(session: requests.Session) -> str:
    header("Step 4 of 4 — device / camera model")
    pr("Scanning your recent photos to detect camera models in your library...")

    models: list[str] = []
    try:
        models = fetch_recent_models(session)
    except Exception:
        warn("Could not fetch recent photos — you'll need to enter the model manually.")

    if models:
        pr(f"\nCamera models found in your library (last 12 months):\n")
        for i, m in enumerate(models, 1):
            print(f"  {BOLD}{i:>3}.{RESET} {m}")
        pr()

        while True:
            raw = prompt("Enter the number of the device to monitor (or type a model name manually)")
            if raw.isdigit():
                idx = int(raw) - 1
                if 0 <= idx < len(models):
                    chosen = models[idx]
                    success(f"Device model set to: {chosen}")
                    return chosen
                else:
                    error(f"Please enter a number between 1 and {len(models)}.")
            elif raw:
                success(f"Device model set to: {raw}")
                return raw
            else:
                error("Please make a selection.")
    else:
        pr("Enter the exact camera model string as it appears in EXIF data.")
        pr("Common examples: Pixel 8, iPhone 15 Pro, SM-S928B\n")
        while True:
            model = prompt("Camera model")
            if model:
                success(f"Device model set to: {model}")
                return model
            error("Model name cannot be empty.")


# ---------------------------------------------------------------------------
# Poll interval
# ---------------------------------------------------------------------------
def step_poll_interval() -> int:
    header("Poll interval")
    pr("How often should the script check for new photos?")
    pr("  Shorter = more real-time, but uses more API quota.")
    pr("  Recommended for a live event: 30 seconds.\n")
    interval = prompt_int("Interval in seconds", 30)
    success(f"Poll interval set to {interval}s.")
    return interval


# ---------------------------------------------------------------------------
# Save & summary
# ---------------------------------------------------------------------------
def save_config(config: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(config, indent=2))
    success(f"Configuration saved to {CONFIG_FILE}")


def print_summary(config: dict) -> None:
    header("Setup complete!")
    pr(f"""
  Album ID      : {config['album_id']}
  Device model  : {config['device_model']}
  Poll interval : {config['poll_interval']}s
  Credentials   : {config['credentials_file']}
""")
    pr(f"{BOLD}To start monitoring, run:{RESET}")
    pr(f"    {GREEN}python monitor.py{RESET}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    print(f"\n{BOLD}{'=' * 55}")
    print("  Google Photos Monitor — Setup Wizard")
    print(f"{'=' * 55}{RESET}")
    pr("This wizard configures the monitor and saves settings to config.json.")
    pr("It will open a browser window to authenticate with your Google account.")

    credentials_file = step_credentials()
    _creds, session = step_auth(credentials_file)
    album_id = step_album(session)
    device_model = step_device(session)
    poll_interval = step_poll_interval()

    config = {
        "album_id": album_id,
        "device_model": device_model,
        "poll_interval": poll_interval,
        "credentials_file": str(credentials_file),
    }

    save_config(config)
    print_summary(config)


if __name__ == "__main__":
    main()
