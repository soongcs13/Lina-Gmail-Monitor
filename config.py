"""Configuration loader for the Palad lead monitor.

Loads secrets from .env and exposes constants used across modules. Nothing in
this file should ever be printed or logged in full — see main.py's logging
setup for redaction rules around GMAIL/NOTION/ANTHROPIC/SLACK secrets.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")


def _path(env_name: str, default: str) -> Path:
    value = os.getenv(env_name, default)
    p = Path(value)
    return p if p.is_absolute() else PROJECT_ROOT / p


# --- Gmail ---
GMAIL_CREDENTIALS_PATH = _path("GMAIL_CREDENTIALS_PATH", "credentials.json")
GMAIL_TOKEN_PATH = _path("GMAIL_TOKEN_PATH", "token.json")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_WATCHED_ADDRESS = os.getenv("GMAIL_WATCHED_ADDRESS", "lina@palad.co")
# On the very first run (no watermark yet), how far back to look instead of
# pulling the entire inbox history. Override via env if you want more/less.
INITIAL_LOOKBACK_DAYS = int(os.getenv("INITIAL_LOOKBACK_DAYS", "7"))

# --- Notion ---
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_VERSION = os.getenv("NOTION_VERSION", "2022-06-28")
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_RATE_LIMIT_PER_SEC = 3

PIPELINE_DB_ID = os.getenv("PIPELINE_DB_ID", "17e6d34b-aa60-438d-b2fe-5b383d566a36")
# The ID in the original brief (3215734a-d555-8015-8f98-ec27cd7249e6) turned
# out to be a page, not the database — confirmed via /v1/search against the
# live integration on 2026-08-07. This is the real database ID.
BD_DATABASE_ID = os.getenv("BD_DATABASE_ID", "3215734a-d555-80f7-a2a1-c4de6375a2ab")

# Pipeline DB property names. These are best guesses from the build brief —
# CONFIRM against the live schema (`python main.py --schema-check`) before
# enabling real writes. Override via env vars if the live schema differs.
PIPELINE_PROP_EMAIL = os.getenv("PIPELINE_PROP_EMAIL", "Email")
# Confirmed against the live schema on 2026-08-07: the title property (the
# lead/company identifier) is called "Name", not "Company Name".
PIPELINE_PROP_COMPANY = os.getenv("PIPELINE_PROP_COMPANY", "Name")
PIPELINE_PROP_STATUS = os.getenv("PIPELINE_PROP_STATUS", "Status")
PIPELINE_PROP_ACTION_LOG = os.getenv("PIPELINE_PROP_ACTION_LOG", "Action Log")

STATUS_BOUNCED = "Bounced"
STATUS_REPLIED = "Replied"
STATUS_CLOSED_LOST = "Closed-Lost"
STATUS_DNC = "Do Not Contact"

# --- Anthropic ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

# --- Slack ---
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

# --- Local state ---
STATE_FILE_PATH = _path("STATE_FILE_PATH", "state/watermark.json")
STAGED_BD_FILE_PATH = _path("STAGED_BD_FILE_PATH", "state/staged_bd_migrations.jsonl")
LOG_FILE_PATH = _path("LOG_FILE_PATH", "logs/palad_lead_monitor.log")

# --- Deterministic classification patterns ---
BOUNCE_SENDER_PATTERN = r"mailer-daemon|postmaster"
BOUNCE_SUBJECT_PATTERN = (
    r"undeliverable|delivery status notification|mail delivery failed|address not found"
)
AUTO_REPLY_SUBJECT_PATTERN = (
    r"out of office|automatic reply|auto-reply|away from my desk|annual leave"
)
AUTO_REPLY_HEADER_NAMES = ("x-autoreply", "x-autorespond")
AUTO_SUBMITTED_PATTERN = r"auto-replied"
PRECEDENCE_BULK_PATTERN = r"\bbulk\b"

# Fuzzy company-name match threshold (difflib ratio, 0-1)
COMPANY_FUZZY_THRESHOLD = 0.6
