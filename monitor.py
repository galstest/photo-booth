#!/usr/bin/env python3
"""
Google Photos monitor — watches for new photos from a specific device
and adds them to a target album in near real-time.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# ---------------------------------------------------------------------------
# Configuration — edit these or override via environment variables
# ---------------------------------------------------------------------------
ALBUM_ID = os.environ.get("ALBUM_ID", "YOUR_ALBUM_ID_HERE")
DEVICE_MODEL = os.environ.get("DEVICE_MODEL", "Pixel 8")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))          # seconds
CHECKPOINT_FILE = Path(os.environ.get("CHECKPOINT_FILE", "checkpoint.json"))
CREDENTIALS_FILE = Path(os.environ.get("CREDENTIALS_FILE", "credentials.json"))
TOKEN_FILE = Path(os.environ.get("TOKEN_FILE", "token.json"))
PAGE_SIZE = 100                                                       # max per API call

SCOPES = ["https://www.googleapis.com/auth/photoslibrary"]

BASE_URL = "https://photoslibrary.googleapis.com/v1"

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
def get_credentials() -> Credentials:
    """Load or refresh OAuth2 credentials, running the browser flow if needed."""
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
                    f"OAuth client secrets file not found: {CREDENTIALS_FILE}\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
            log.info("Authentication successful.")

        TOKEN_FILE.write_text(creds.to_json())

    return creds


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------
def load_checkpoint() -> datetime:
    """Return the last processed timestamp, or 'now' on first run."""
    if CHECKPOINT_FILE.exists():
        data = json.loads(CHECKPOINT_FILE.read_text())
        ts = datetime.fromisoformat(data["last_processed_timestamp"])
        log.info("Resuming from checkpoint: %s", ts.isoformat())
        return ts

    # First run — only pick up photos taken from this moment forward.
    ts = datetime.now(timezone.utc)
    save_checkpoint(ts)
    log.info("No checkpoint found. Starting from now: %s", ts.isoformat())
    return ts


def save_checkpoint(ts: datetime) -> None:
    CHECKPOINT_FILE.write_text(
        json.dumps({"last_processed_timestamp": ts.isoformat()}, indent=2)
    )


# ---------------------------------------------------------------------------
# Google Photos API helpers
# ---------------------------------------------------------------------------
def _auth_session(creds: Credentials) -> requests.Session:
    """Return a requests Session with a valid Bearer token header."""
    # Refresh if needed before building the header
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {creds.token}"})
    return session


def search_media_items(
    session: requests.Session,
    after: datetime,
    page_token: str | None = None,
) -> dict:
    """
    Call mediaItems.search with a date filter covering [after, now].
    Returns the raw API response dict.
    """
    now = datetime.now(timezone.utc)

    body: dict = {
        "pageSize": PAGE_SIZE,
        "filters": {
            "dateFilter": {
                "ranges": [
                    {
                        "startDate": {
                            "year": after.year,
                            "month": after.month,
                            "day": after.day,
                        },
                        "endDate": {
                            "year": now.year,
                            "month": now.month,
                            "day": now.day,
                        },
                    }
                ]
            },
            "mediaTypeFilter": {"mediaTypes": ["PHOTO"]},
        },
    }

    if page_token:
        body["pageToken"] = page_token

    resp = session.post(f"{BASE_URL}/mediaItems:search", json=body, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_media_item(session: requests.Session, media_item_id: str) -> dict:
    """Fetch full metadata for a single media item (needed for EXIF model tag)."""
    resp = session.get(f"{BASE_URL}/mediaItems/{media_item_id}", timeout=15)
    resp.raise_for_status()
    return resp.json()


def batch_add_to_album(
    session: requests.Session, album_id: str, media_item_ids: list[str]
) -> None:
    """Add up to 50 media items to an album in one call."""
    # API limit: max 50 items per batchAdd call
    chunk_size = 50
    for i in range(0, len(media_item_ids), chunk_size):
        chunk = media_item_ids[i : i + chunk_size]
        body = {"mediaItemIds": chunk}
        resp = session.post(
            f"{BASE_URL}/albums/{album_id}:batchAddMediaItems",
            json=body,
            timeout=30,
        )
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def matches_device(item: dict, model: str) -> bool:
    """Return True if the item's camera model matches (case-insensitive)."""
    photo_meta = (
        item.get("mediaMetadata", {})
        .get("photo", {})
    )
    item_model = photo_meta.get("cameraModel", "")
    return item_model.lower() == model.lower()


def creation_time(item: dict) -> datetime:
    raw = item.get("mediaMetadata", {}).get("creationTime", "")
    return datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else datetime.min.replace(tzinfo=timezone.utc)


def poll(creds: Credentials, since: datetime) -> datetime:
    """
    One poll cycle. Returns the updated 'since' timestamp (the newest item seen,
    or the original value if nothing new was found).
    """
    log.info("Searching for new photos since %s (device: %s)...", since.isoformat(), DEVICE_MODEL)

    session = _auth_session(creds)
    matched_ids: list[str] = []
    newest_ts = since
    page_token: str | None = None

    while True:
        data = search_media_items(session, after=since, page_token=page_token)
        items = data.get("mediaItems", [])

        for item in items:
            item_ts = creation_time(item)

            # Skip anything not newer than our checkpoint
            if item_ts <= since:
                continue

            # The search result includes basic photo metadata; cameraModel is included.
            if matches_device(item, DEVICE_MODEL):
                matched_ids.append(item["id"])
                log.debug("  Match: %s  [%s]", item.get("filename"), item_ts.isoformat())

            if item_ts > newest_ts:
                newest_ts = item_ts

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    if matched_ids:
        log.info("Found %d new photo(s) from '%s'. Adding to album...", len(matched_ids), DEVICE_MODEL)
        batch_add_to_album(session, ALBUM_ID, matched_ids)
        log.info("Successfully added %d photo(s) to album.", len(matched_ids))
    else:
        log.info("No new matching photos found.")

    return newest_ts


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    if ALBUM_ID == "YOUR_ALBUM_ID_HERE":
        raise SystemExit(
            "Set the ALBUM_ID environment variable (or edit the constant at the top of monitor.py)."
        )

    log.info("=== Google Photos Monitor starting ===")
    log.info("  Album ID    : %s", ALBUM_ID)
    log.info("  Device model: %s", DEVICE_MODEL)
    log.info("  Poll interval: %ds", POLL_INTERVAL)

    creds = get_credentials()
    since = load_checkpoint()

    while True:
        try:
            newest = poll(creds, since)
            if newest > since:
                since = newest
                save_checkpoint(since)
        except requests.exceptions.Timeout:
            log.warning("Request timed out — will retry next cycle.")
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            if status == 429:
                log.warning("API quota limit hit (429). Backing off for 60s...")
                time.sleep(60)
            elif status in (500, 502, 503, 504):
                log.warning("Server error (%s). Will retry next cycle.", status)
            else:
                log.error("HTTP error %s: %s", status, exc)
        except requests.exceptions.ConnectionError:
            log.warning("Network connection error — will retry next cycle.")
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected error: %s", exc)

        log.info("Sleeping %ds...\n", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
