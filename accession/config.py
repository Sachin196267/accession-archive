"""Runtime configuration.

Everything that changes per installation lives in the `settings` table so it can be
edited from the web interface. Environment variables only decide where data lives and
can override the Internet Archive keys (handy for CI or a shared server).
"""
import os
from pathlib import Path

VERSION = "1.0.0"

# Serverless hosts (Vercel) have a read-only disk apart from /tmp and no process that
# outlives a request: the repository then lives in a hosted libSQL database and the
# background work runs in short bursts (see worker.Engine.burst).
SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("ACCESSION_SERVERLESS"))

HOME = Path(os.environ.get("ACCESSION_HOME", "/tmp/accession" if SERVERLESS else Path.cwd() / "data")).resolve()
DB_PATH = Path(os.environ.get("ACCESSION_DB", HOME / "accession.db"))
DB_URL = os.environ.get("ACCESSION_DB_URL") or os.environ.get("TURSO_DATABASE_URL") or ""
DB_TOKEN = os.environ.get("ACCESSION_DB_TOKEN") or os.environ.get("TURSO_AUTH_TOKEN") or ""
SNAPSHOT_DIR = HOME / "snapshots"
# keep snapshot bodies in the database when there is no lasting disk
SNAPSHOTS_IN_DB = bool(DB_URL) or SERVERLESS

# Optional shared password for the web interface (recommended on a public host).
PASSWORD = os.environ.get("ACCESSION_PASSWORD", "")
SECRET = os.environ.get("ACCESSION_SECRET", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")

# Public mode: anyone may browse and add sites; ACCESSION_PASSWORD then only guards
# owner actions (deleting domains, system settings, pausing services) and visitors
# get conservative limits. Fetches to private / internal addresses are refused.
PUBLIC = os.environ.get("ACCESSION_PUBLIC") == "1"

# Time zone for dates shown in the interface. Servers (Vercel) run on UTC, so the
# hosted site defaults to Indian Standard Time; locally the machine's zone is used.
DISPLAY_TZ = os.environ.get("ACCESSION_TZ") or ("Asia/Kolkata" if SERVERLESS else "")
PUBLIC_MAX_PAGES = int(os.environ.get("ACCESSION_PUBLIC_MAX_PAGES", 500))
PUBLIC_MAX_DOMAINS = int(os.environ.get("ACCESSION_PUBLIC_MAX_DOMAINS", 100))

USER_AGENT = f"Accession/{VERSION} (website archival tool; respects robots.txt)"

# Defaults for the settings table. Values are strings; see settings.get_* helpers.
DEFAULT_SETTINGS = {
    "queue_paused": "0",
    "max_parallel_scans": "4",
    "max_attempts": "5",
    "retry_base_seconds": "30",
    "fetch_timeout": "20",
    # Internet Archive / Save Page Now 2
    "ia_access_key": "",
    "ia_secret_key": "",
    "wayback_base": "https://web.archive.org",
    "wayback_reuse_days": "0",  # 0 = always request a fresh capture
    "wayback_concurrency": "1",
    "wayback_interval": "21",  # anonymous SPN2 allows ~3 captures / minute
    "wayback_interval_auth": "9",  # authenticated: ~7 / minute
    "wayback_poll_timeout": "240",
    "wayback_poll_interval": "3",
    # archive.today
    "archive_today_host": "archive.ph",
    "archive_today_concurrency": "1",
    "archive_today_interval": "30",
    # local snapshot store
    "local_concurrency": "4",
    "local_interval": "0",
    # serverless burst lease (only one burst works the queue at a time)
    "burst_until": "0",
}


def ia_keys(settings: dict) -> tuple[str, str]:
    access = os.environ.get("IA_ACCESS_KEY") or settings.get("ia_access_key", "")
    secret = os.environ.get("IA_SECRET_KEY") or settings.get("ia_secret_key", "")
    return access.strip(), secret.strip()
