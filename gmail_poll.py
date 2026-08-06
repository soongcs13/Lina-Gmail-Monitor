"""Gmail OAuth handling and inbox polling.

Read-only scope only (gmail.readonly). This module never sends or modifies
mail. Watermark state (last processed internalDate + message IDs seen at
that exact timestamp) is persisted locally so re-runs never reprocess the
same message.
"""
import base64
import json
import logging
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config

log = logging.getLogger("gmail_poll")


@dataclass
class GmailMessage:
    id: str
    thread_id: str
    internal_date_ms: int
    from_email: str
    from_name: str
    subject: str
    headers: dict = field(default_factory=dict)  # lowercased header name -> value
    body_text: str = ""
    has_delivery_status_part: bool = False
    raw_from: str = ""


def get_gmail_service():
    """Returns an authenticated Gmail API service, running the OAuth
    browser flow on first use and persisting/refreshing token.json after."""
    creds: Optional[Credentials] = None

    if config.GMAIL_TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(
            str(config.GMAIL_TOKEN_PATH), config.GMAIL_SCOPES
        )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                log.critical(
                    "Gmail token refresh failed — token likely revoked. "
                    "Delete token.json and re-run interactively to re-auth. Error: %s",
                    exc,
                )
                raise
        else:
            if not config.GMAIL_CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    f"Gmail OAuth client file not found at {config.GMAIL_CREDENTIALS_PATH}. "
                    "Place the Desktop-app credentials.json there before first run."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(config.GMAIL_CREDENTIALS_PATH), config.GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)

        _atomic_write(config.GMAIL_TOKEN_PATH, creds.to_json())
        log.info("Wrote/refreshed Gmail token at %s", config.GMAIL_TOKEN_PATH)

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent)
    try:
        with open(fd, "w") as f:
            f.write(content)
        Path(tmp_path).replace(path)
    finally:
        if Path(tmp_path).exists():
            Path(tmp_path).unlink()


def load_state() -> dict:
    if config.STATE_FILE_PATH.exists():
        try:
            return json.loads(config.STATE_FILE_PATH.read_text())
        except json.JSONDecodeError:
            log.error("State file %s is corrupt, starting from empty watermark", config.STATE_FILE_PATH)
    return {"last_internal_date_ms": 0, "processed_ids_at_last_date": []}


def save_state(state: dict):
    _atomic_write(config.STATE_FILE_PATH, json.dumps(state, indent=2))


def advance_state(state: dict, message: GmailMessage) -> dict:
    """Returns a new state dict reflecting `message` having been fully processed."""
    if message.internal_date_ms > state.get("last_internal_date_ms", 0):
        return {
            "last_internal_date_ms": message.internal_date_ms,
            "processed_ids_at_last_date": [message.id],
        }
    ids = set(state.get("processed_ids_at_last_date", []))
    ids.add(message.id)
    return {
        "last_internal_date_ms": state["last_internal_date_ms"],
        "processed_ids_at_last_date": sorted(ids),
    }


def _header_value(headers: list, name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _parse_email_address(raw: str) -> tuple:
    """Returns (display_name, email_address) from a From/Reply-To header value."""
    import email.utils

    name, addr = email.utils.parseaddr(raw)
    return name, addr.lower()


def _find_delivery_status_part(payload: dict) -> bool:
    if not payload:
        return False
    if payload.get("mimeType") == "message/delivery-status":
        return True
    for part in payload.get("parts", []) or []:
        if _find_delivery_status_part(part):
            return True
    return False


def _extract_body_text(payload: dict) -> str:
    if not payload:
        return ""

    def decode(data: str) -> str:
        try:
            return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")
        except Exception:
            return ""

    mime_type = payload.get("mimeType", "")
    body = payload.get("body", {})

    if mime_type == "text/plain" and body.get("data"):
        return decode(body["data"])

    parts = payload.get("parts", []) or []
    # Prefer text/plain, fall back to text/html stripped of tags.
    for part in parts:
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return decode(part["body"]["data"])
    for part in parts:
        text = _extract_body_text(part)
        if text:
            return text

    if mime_type == "text/html" and body.get("data"):
        import re as _re

        html = decode(body["data"])
        return _re.sub(r"<[^>]+>", " ", html)

    return ""


def parse_message(raw_message: dict) -> GmailMessage:
    payload = raw_message.get("payload", {})
    headers_list = payload.get("headers", [])
    headers = {h.get("name", "").lower(): h.get("value", "") for h in headers_list}

    raw_from = headers.get("from", "")
    from_name, from_email = _parse_email_address(raw_from)
    subject = headers.get("subject", "")

    return GmailMessage(
        id=raw_message["id"],
        thread_id=raw_message.get("threadId", ""),
        internal_date_ms=int(raw_message.get("internalDate", "0")),
        from_email=from_email,
        from_name=from_name,
        subject=subject,
        headers=headers,
        body_text=_extract_body_text(payload),
        has_delivery_status_part=_find_delivery_status_part(payload),
        raw_from=raw_from,
    )


def poll_new_messages(service, state: dict) -> list:
    """Fetches inbox messages newer than the watermark in `state`, oldest first."""
    watermark_ms = state.get("last_internal_date_ms", 0)
    processed_at_watermark = set(state.get("processed_ids_at_last_date", []))

    query = "in:inbox"
    if watermark_ms:
        # Gmail's `after:` is day-granularity; go back a day for a safety margin
        # and rely on internalDate for exact filtering below.
        buffer_dt = datetime.utcfromtimestamp(watermark_ms / 1000) - timedelta(days=1)
        query += f" after:{int(buffer_dt.timestamp())}"

    message_ids = []
    page_token = None
    while True:
        try:
            resp = service.users().messages().list(
                userId="me", q=query, pageToken=page_token, maxResults=100
            ).execute()
        except HttpError as exc:
            log.error("Gmail messages.list failed: %s", exc)
            raise
        message_ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    messages = []
    for mid in message_ids:
        try:
            raw = service.users().messages().get(userId="me", id=mid, format="full").execute()
        except HttpError as exc:
            log.error("Gmail messages.get failed for %s: %s", mid, exc)
            raise
        msg = parse_message(raw)
        if msg.internal_date_ms < watermark_ms:
            continue
        if msg.internal_date_ms == watermark_ms and msg.id in processed_at_watermark:
            continue
        messages.append(msg)

    messages.sort(key=lambda m: m.internal_date_ms)
    return messages


if __name__ == "__main__":
    # Build order step 1: confirm OAuth + token persistence, print raw
    # message metadata only. No classification, no writes.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    svc = get_gmail_service()
    st = load_state()
    msgs = poll_new_messages(svc, st)
    print(f"Found {len(msgs)} new message(s) since watermark {st.get('last_internal_date_ms', 0)}")
    for m in msgs:
        print(json.dumps({
            "id": m.id,
            "thread_id": m.thread_id,
            "internal_date_ms": m.internal_date_ms,
            "from": m.raw_from,
            "subject": m.subject,
            "has_delivery_status_part": m.has_delivery_status_part,
        }, indent=2))
