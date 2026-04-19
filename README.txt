Google Photos Monitor — Bat Mitzvah Edition
============================================

Monitors a Google Photos library and automatically adds photos taken by a
specific device into a target Google Photos album in near real-time.


HOW IT WORKS
------------
1. Checkpoint  — checkpoint.json stores last_processed_timestamp.
                 First run sets it to "now" so old library photos are ignored.

2. Poll loop   — Every POLL_INTERVAL seconds, calls mediaItems:search with a
                 date-range filter (since → today). Paginates all result pages.

3. Device filter — Checks mediaMetadata.photo.cameraModel against DEVICE_MODEL
                   (case-insensitive). The field is returned inline — no
                   separate EXIF download needed.

4. Add to album — Calls albums/{id}:batchAddMediaItems in chunks of 50
                  (Google API limit per request).

5. Checkpoint update — Advances last_processed_timestamp to the newest item
                       seen so duplicates are never re-added.

6. Error handling — Catches timeouts, 429 (quota), 5xx server errors, and
                    connection drops without crashing the process.


SETUP (one-time)
----------------
1. Go to Google Cloud Console and enable the Google Photos Library API.

2. Create an OAuth 2.0 Client ID (type: Desktop app).
   Download the JSON file and save it as credentials.json in this folder.

3. Install Python dependencies:

       pip install -r requirements.txt

4. Find your Album ID by running this snippet after auth is working:

       python - <<'EOF'
       from monitor import get_credentials, _auth_session, BASE_URL
       session = _auth_session(get_credentials())
       r = session.get(f"{BASE_URL}/albums", params={"pageSize": 50})
       for a in r.json().get("albums", []):
           print(a["id"], a.get("title"))
       EOF


RUNNING
-------
Set the required environment variables, then run monitor.py:

    export ALBUM_ID="your_bat_mitzvah_album_id_here"
    export DEVICE_MODEL="Pixel 8"     # must match cameraModel in EXIF exactly
    export POLL_INTERVAL=30           # seconds between checks (default: 30)

    python monitor.py

The first run opens a browser for the OAuth consent screen. After that,
token.json is stored locally and refreshed silently on subsequent runs.


ENVIRONMENT VARIABLES
---------------------
  ALBUM_ID          Target Google Photos album ID (required)
  DEVICE_MODEL      Camera model string to match, e.g. "Pixel 8" (default: "Pixel 8")
  POLL_INTERVAL     Seconds between each search cycle (default: 30)
  CHECKPOINT_FILE   Path to the checkpoint file (default: checkpoint.json)
  CREDENTIALS_FILE  Path to the OAuth client secrets file (default: credentials.json)
  TOKEN_FILE        Path to the cached token file (default: token.json)


FILES
-----
  monitor.py        Main script
  requirements.txt  Python dependencies
  credentials.json  OAuth client secrets — download from Google Cloud Console
                    (NOT committed to version control)
  token.json        Cached OAuth token — auto-generated on first run
                    (NOT committed to version control)
  checkpoint.json   Last-processed timestamp — auto-generated on first run


REQUIREMENTS
------------
  google-auth==2.29.0
  google-auth-oauthlib==1.2.0
  google-auth-httplib2==0.2.0
  requests==2.32.3


NOTES
-----
- The Google Photos Library API does not support filtering by device name
  directly. This script filters by creationTime (via dateFilter) and then
  checks mediaMetadata.photo.cameraModel on each returned item.

- The API scope used is https://www.googleapis.com/auth/photoslibrary.
  This grants read/write access to the library. Keep credentials.json and
  token.json out of version control.
