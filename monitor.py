#!/usr/bin/env python3
"""
Google Photos monitor (Drive API edition) — watches for new photos from a
specific device in the Drive-synced Google Photos folder and copies them
into a target Drive folder in near real-time.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---------------------------------------------------------------------------
# Configuration — loaded from config.json (created by setup.py)
# ---------------------------------------------------------------------------
CONFIG_FILE = Path("config.json")


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


_cfg = _load_config()

SOURCE_FOLDER_ID = os.environ.get("SOURCE_FOLDER_ID", _cfg.get("source_folder_id", ""))
TARGET_FOLDER_ID = os.environ.get("TARGET_FOLDER_ID", _cfg.get("target_folder_id", ""))
DEVICE_MODEL     = os.environ.get("DEVICE_MODEL",     _cfg.get("device_model",     "Pixel 8"))
POLL_INTERVAL    = int(os.environ.get("POLL_INTERVAL", str(_cfg.get("poll_interval", 30))))
CREDENTIALS_FILE = Path(os.environ.get("CREDENTIALS_FILE", _cfg.get("credentials_file", "credentials.json")))
CHECKPOINT_FILE  = Path(os.environ.get("CHECKPOINT_FILE", "checkpoint.json"))
TOKEN_FILE       = Path("token.json")

SCOPES = ["https://www.googleapis.com/auth/drive"]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def get_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log.info("Refreshing access token...")
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"OAuth client secrets not found: {CREDENTIALS_FILE}\n"
                    "Run python3 setup.py first."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
            log.info("Authentication successful.")
        TOKEN_FILE.write_text(creds.to_json())

    return build("drive", "v3", credentials=creds)


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------
def load_checkpoint() -> datetime:
    if CHECKPOINT_FILE.exists():
        data = json.loads(CHECKPOINT_FILE.read_text())
        ts = datetime.fromisoformat(data["last_processed_timestamp"])
        log.info("Resuming from checkpoint: %s", ts.isoformat())
        return ts

    ts = datetime.now(timezone.utc)
    save_checkpoint(ts)
    log.info("No checkpoint found. Starting from now: %s", ts.isoformat())
    return ts


def save_checkpoint(ts: datetime) -> None:
    CHECKPOINT_FILE.write_text(
        json.dumps({"last_processed_timestamp": ts.isoformat()}, indent=2)
    )


# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------
def list_new_photos(service, since: datetime) -> list[dict]:
    """
    List image files in the source folder created after `since`,
    including their imageMediaMetadata for camera model filtering.
    """
    since_str = since.strftime("%Y-%m-%dT%H:%M:%S")
    query = (
        f"mimeType contains 'image/' "
        f"and trashed = false "
        f"and createdTime > '{since_str}'"
    )
    if SOURCE_FOLDER_ID:
        query += f" and '{SOURCE_FOLDER_ID}' in parents"

    files = []
    page_token = None
    while True:
        resp = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, createdTime, imageMediaMetadata)",
            pageSize=100,
            orderBy="createdTime asc",
            pageToken=page_token,
        ).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return files


def matches_device(file: dict, model: str) -> bool:
    item_model = (file.get("imageMediaMetadata") or {}).get("cameraModel", "")
    return item_model.lower() == model.lower()


def copy_to_target(service, file_id: str, target_folder_id: str) -> None:
    """Add the file to the target folder (without removing it from source)."""
    service.files().update(
        fileId=file_id,
        addParents=target_folder_id,
        fields="id, parents",
    ).execute()


# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------
def poll(service, since: datetime) -> datetime:
    log.info("Searching for new photos since %s (device: %s)...", since.isoformat(), DEVICE_MODEL)

    files = list_new_photos(service, since)
    newest_ts = since
    matched = []

    for f in files:
        raw_ts = f.get("createdTime", "")
        file_ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")) if raw_ts else since

        if file_ts > newest_ts:
            newest_ts = file_ts

        if matches_device(f, DEVICE_MODEL):
            matched.append(f)
            log.debug("  Match: %s [%s]", f.get("name"), file_ts.isoformat())

    if matched:
        log.info("Found %d new photo(s) from '%s'. Adding to folder...", len(matched), DEVICE_MODEL)
        for f in matched:
            copy_to_target(service, f["id"], TARGET_FOLDER_ID)
        log.info("Successfully added %d photo(s) to target folder.", len(matched))
    else:
        log.info("No new matching photos found.")

    return newest_ts


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    if not TARGET_FOLDER_ID:
        raise SystemExit(
            "No target folder configured.\n"
            "Run the setup wizard first:\n\n"
            "    python3 setup.py\n"
        )

    log.info("=== Google Photos Monitor (Drive API) starting ===")
    log.info("  Source folder: %s", SOURCE_FOLDER_ID or "all of Drive")
    log.info("  Target folder: %s", TARGET_FOLDER_ID)
    log.info("  Device model : %s", DEVICE_MODEL)
    log.info("  Poll interval: %ds", POLL_INTERVAL)

    service = get_service()
    since = load_checkpoint()

    while True:
        try:
            newest = poll(service, since)
            if newest > since:
                since = newest
                save_checkpoint(since)
        except HttpError as exc:
            status = exc.resp.status
            if status == 429:
                log.warning("API quota limit (429). Backing off 60s...")
                time.sleep(60)
            elif status in (500, 502, 503, 504):
                log.warning("Server error (%s). Retrying next cycle.", status)
            else:
                log.error("HTTP error %s: %s", status, exc)
        except TimeoutError:
            log.warning("Request timed out — retrying next cycle.")
        except ConnectionError:
            log.warning("Network error — retrying next cycle.")
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected error: %s", exc)

        log.info("Sleeping %ds...\n", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
