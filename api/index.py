"""Vercel entry point: the whole app is one Python function.

Vercel runs this ASGI app per request. There is no lasting disk or background
process there, so set TURSO_DATABASE_URL / TURSO_AUTH_TOKEN (the repository) and
ACCESSION_PASSWORD (the login). Background work runs in bursts via /api/tick,
driven by open pages and by the cron job in vercel.json.
"""
from accession.web.app import app  # noqa: F401
