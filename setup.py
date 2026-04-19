#!/usr/bin/env python3
"""
Interactive setup wizard for the Google Photos → Drive monitor.
Run once before starting monitor.py:

    python3 setup.py
"""

import json
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

CONFIG_FILE = Path("config.json")
TOKEN_FILE = Path("token.json")
SCOPES = ["https://www.googleapis.com/auth/drive"]

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
        "  2. Enable the Google Drive API\n"
        "  3. APIs & Services → Credentials → Create Credentials → OAuth client ID\n"
        "  4. Application type: Desktop app\n"
        "  5. Download the JSON and save it somewhere accessible\n"
    )

    while True:
        path_str = prompt("Path to credentials JSON file", "credentials.json")
        path = Path(path_str).expanduser()
        if path.exists():
            try:
                data = json.loads(path.read_text())
                if "installed" not in data and "web" not in data:
                    error("Doesn't look like a valid OAuth client secrets file.")
                    continue
                success(f"Found credentials: {path}")
                return path
            except json.JSONDecodeError:
                error("File is not valid JSON.")
        else:
            error(f"File not found: {path}")


# ---------------------------------------------------------------------------
# Step 2 — OAuth
# ---------------------------------------------------------------------------
def step_auth(credentials_file: Path):
    header("Step 2 of 4 — Google account authentication")
    pr("A browser window will open for you to sign in and grant Drive access.\n")

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
    return build("drive", "v3", credentials=creds)


# ---------------------------------------------------------------------------
# Step 3 — pick target folder
# ---------------------------------------------------------------------------
def find_google_photos_folder(service) -> str | None:
    """Return the Drive folder ID of the auto-synced 'Google Photos' folder."""
    results = service.files().list(
        q="name = 'Google Photos' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
        fields="files(id, name)",
        pageSize=5,
    ).execute()
    files = results.get("files", [])
    return files[0]["id"] if files else None


def list_folders(service) -> list[dict]:
    folders = []
    page_token = None
    while True:
        resp = service.files().list(
            q="mimeType = 'application/vnd.google-apps.folder' and trashed = false",
            fields="nextPageToken, files(id, name)",
            pageSize=100,
            pageToken=page_token,
        ).execute()
        folders.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return sorted(folders, key=lambda f: f["name"].lower())


def step_target_folder(service) -> str:
    header("Step 3 of 4 — target Drive folder")
    pr("Fetching your Google Drive folders...")

    folders = list_folders(service)

    if not folders:
        warn("No folders found. Enter a folder ID manually.")
        return prompt("Target folder ID")

    pr(f"\nFound {len(folders)} folder(s):\n")
    for i, f in enumerate(folders, 1):
        print(f"  {BOLD}{i:>3}.{RESET} {f['name']}")

    pr()
    while True:
        raw = prompt("Enter the number of the Bat Mitzvah folder (or paste a folder ID)")
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(folders):
                chosen = folders[idx]
                success(f"Target folder: {chosen['name']}")
                return chosen["id"]
            error(f"Enter a number between 1 and {len(folders)}.")
        elif raw:
            success(f"Using folder ID: {raw}")
            return raw
        else:
            error("Please make a selection.")


# ---------------------------------------------------------------------------
# Step 4 — device model
# ---------------------------------------------------------------------------
def fetch_recent_models(service) -> list[str]:
    """Sample recent Drive images and return unique cameraModel values."""
    resp = service.files().list(
        q="mimeType contains 'image/' and trashed = false",
        fields="files(id, imageMediaMetadata)",
        pageSize=200,
        orderBy="createdTime desc",
    ).execute()

    seen: dict[str, int] = {}
    for f in resp.get("files", []):
        model = (f.get("imageMediaMetadata") or {}).get("cameraModel", "")
        if model:
            seen[model] = seen.get(model, 0) + 1
    return sorted(seen, key=lambda m: seen[m], reverse=True)


def step_device(service) -> str:
    header("Step 4 of 4 — device / camera model")
    pr("Scanning recent Drive photos for camera models...")

    models: list[str] = []
    try:
        models = fetch_recent_models(service)
    except Exception:
        warn("Could not scan photos — you'll enter the model manually.")

    if models:
        pr(f"\nCamera models found in your Drive:\n")
        for i, m in enumerate(models, 1):
            print(f"  {BOLD}{i:>3}.{RESET} {m}")
        pr()

        while True:
            raw = prompt("Enter the number of the device to monitor (or type a model name)")
            if raw.isdigit():
                idx = int(raw) - 1
                if 0 <= idx < len(models):
                    chosen = models[idx]
                    success(f"Device model: {chosen}")
                    return chosen
                error(f"Enter a number between 1 and {len(models)}.")
            elif raw:
                success(f"Device model: {raw}")
                return raw
            else:
                error("Please make a selection.")
    else:
        pr("Enter the exact camera model as it appears in EXIF data.")
        pr("Examples: Pixel 8, iPhone 15 Pro, SM-S928B\n")
        while True:
            model = prompt("Camera model")
            if model:
                success(f"Device model: {model}")
                return model
            error("Model name cannot be empty.")


# ---------------------------------------------------------------------------
# Poll interval
# ---------------------------------------------------------------------------
def step_poll_interval() -> int:
    header("Poll interval")
    pr("How often should the script check for new photos?")
    pr("Recommended for a live event: 30 seconds.\n")
    interval = prompt_int("Interval in seconds", 30)
    success(f"Poll interval: {interval}s")
    return interval


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    print(f"\n{BOLD}{'=' * 55}")
    print("  Google Photos Monitor — Setup Wizard")
    print(f"{'=' * 55}{RESET}")
    pr("Configures the Drive-based photo monitor and saves to config.json.")

    credentials_file = step_credentials()
    service = step_auth(credentials_file)

    # Auto-detect the Google Photos folder in Drive
    header("Detecting Google Photos folder in Drive...")
    gp_folder_id = find_google_photos_folder(service)
    if gp_folder_id:
        success(f"Found 'Google Photos' folder in Drive (ID: {gp_folder_id})")
    else:
        warn("Could not find a 'Google Photos' folder in your Drive.")
        pr("Make sure Google Photos backup is enabled on your phone.")
        pr("You can also enter a source folder ID manually.\n")
        gp_folder_id = prompt("Source folder ID to monitor")

    target_folder_id = step_target_folder(service)
    device_model = step_device(service)
    poll_interval = step_poll_interval()

    config = {
        "source_folder_id": gp_folder_id,
        "target_folder_id": target_folder_id,
        "device_model": device_model,
        "poll_interval": poll_interval,
        "credentials_file": str(credentials_file),
    }

    CONFIG_FILE.write_text(json.dumps(config, indent=2))
    success(f"Configuration saved to {CONFIG_FILE}")

    print(f"\n{BOLD}Setup complete!{RESET}")
    print(f"""
  Source folder : Google Photos (Drive)
  Target folder : {target_folder_id}
  Device model  : {device_model}
  Poll interval : {poll_interval}s
""")
    print(f"{BOLD}To start monitoring, run:{RESET}")
    print(f"    {GREEN}python3 monitor.py{RESET}\n")


if __name__ == "__main__":
    main()
