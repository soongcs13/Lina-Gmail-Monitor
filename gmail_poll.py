"""Gmail OAuth handling and inbox polling.

Uses gmail.modify scope (only to change the UNREAD label based on
classification) — never used to send, reply to, or delete mail. Watermark
state (last processed internalDate + message IDs seen at that exact
timestamp) is persisted locally so re-runs never reprocess the same
message.
"""
import base64
import json
import logging
import re
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
    # For bounces only: the original recipient address that failed to
    # deliver, extracted from the delivery-status part (or best-effort
    # fallbacks). This — not from_email, which is mailer-daemon — is what
    # must be matched against Pipeline leads.
    bounced_recipient: str = ""


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


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _get_delivery_status_text(payload: dict) -> str:
    """Returns the decoded body of the message/delivery-status MIME part, if any."""
    if not payload:
        return ""
    if payload.get("mimeType") == "message/delivery-status":
        data = payload.get("body", {}).get("data")
        if data:
            try:
                return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")
            except Exception:
                return ""
    for part in payload.get("parts", []) or []:
        text = _get_delivery_status_text(part)
        if text:
            return text
    return ""


def _extract_bounced_recipient(payload: dict, headers: dict, body_text: str) -> str:
    """For a bounce message, finds the original recipient address that
    failed to deliver — NOT the mailer-daemon sender. That's what must be
    matched against Pipeline leads. Tries, in order: the machine-readable
    delivery-status part, the X-Failed-Recipients header, then a best-effort
    scan of the human-readable body text."""
    ds_text = _get_delivery_status_text(payload)
    if ds_text:
        match = re.search(r"(?:Final|Original)-Recipient:\s*(?:rfc822|RFC822)\s*;\s*(\S+)", ds_text)
        if match:
            return match.group(1).strip().rstrip(".").lower()

    failed_header = headers.get("x-failed-recipients", "")
    if failed_header:
        return failed_header.split(",")[0].strip().lower()

    for line in body_text.splitlines():
        if re.search(r"delivered to|recipient|address", line, re.I):
            m = _EMAIL_RE.search(line)
            if m:
                candidate = m.group(0).lower()
                if "mailer-daemon" not in candidate and "postmaster" not in candidate:
                    return candidate

    return ""


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
    body_text = _extract_body_text(payload)
    has_delivery_status = _find_delivery_status_part(payload)

    # Attempted unconditionally: classify_deterministic() can flag a message
    # as a bounce via subject text alone (no delivery-status part present),
    # so gating this on has_delivery_status would miss those. It's a no-op
    # cost for non-bounce messages — the result is simply unused.
    bounced_recipient = _extract_bounced_recipient(payload, headers, body_text)

    return GmailMessage(
        id=raw_message["id"],
        thread_id=raw_message.get("threadId", ""),
        internal_date_ms=int(raw_message.get("internalDate", "0")),
        from_email=from_email,
        from_name=from_name,
        subject=subject,
        headers=headers,
        body_text=body_text,
        has_delivery_status_part=has_delivery_status,
        raw_from=raw_from,
        bounced_recipient=bounced_recipient,
    )


def poll_new_messages(service, state: dict) -> list:
    """Fetches inbox messages newer than the watermark in `state`, oldest first."""
    watermark_ms = state.get("last_internal_date_ms", 0)
    processed_at_watermark = set(state.get("processed_ids_at_last_date", []))

    if watermark_ms == 0:
        # First run ever, no watermark yet: bound to a recent lookback window
        # rather than pulling/reprocessing the entire inbox history.
        lookback_dt = datetime.utcnow() - timedelta(days=config.INITIAL_LOOKBACK_DAYS)
        watermark_ms = int(lookback_dt.timestamp() * 1000)
        log.info("No prior watermark found — bounding first run to the last %d day(s)",
                  config.INITIAL_LOOKBACK_DAYS)

    # Gmail's `after:` is day-granularity; go back a day for a safety margin
    # and rely on internalDate for exact filtering below.
    buffer_dt = datetime.utcfromtimestamp(watermark_ms / 1000) - timedelta(days=1)
    query = f"in:inbox after:{int(buffer_dt.timestamp())}"

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


def mark_read(service, message_id: str):
    """Removes the UNREAD label. Used for bounces, auto-replies, and
    Irrelevant messages — nothing the user needs to look at."""
    try:
        service.users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()
    except HttpError as exc:
        log.error("Failed to mark message %s as read: %s", message_id, exc)
        raise


def mark_unread(service, message_id: str):
    """Adds the UNREAD label. Used for genuine replies that need the user's
    attention (interested/declined/unsubscribe/unclear/BD re-engagement)."""
    try:
        service.users().messages().modify(
            userId="me", id=message_id, body={"addLabelIds": ["UNREAD"]}
        ).execute()
    except HttpError as exc:
        log.error("Failed to mark message %s as unread: %s", message_id, exc)
        raise


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
