"""Notion REST API access: schema inspection, Pipeline lead matching, and
read-modify-append writes. Throttled to Notion's ~3 req/s limit. Every
non-2xx response raises — writes never fail silently.
"""
import difflib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime

import requests

import config

log = logging.getLogger("notion_write")


class NotionAPIError(Exception):
    def __init__(self, method: str, path: str, status: int, body: str):
        self.method = method
        self.path = path
        self.status = status
        self.body = body
        super().__init__(f"Notion API {method} {path} -> {status}: {body[:500]}")


@dataclass
class PipelineLead:
    page_id: str
    email: str
    company_name: str
    status: str
    action_log_text: str


class NotionClient:
    def __init__(self, token: str = None):
        self.token = token or config.NOTION_TOKEN
        if not self.token:
            raise RuntimeError("NOTION_TOKEN is not set")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": config.NOTION_VERSION,
            "Content-Type": "application/json",
        })
        self._min_interval = 1.0 / config.NOTION_RATE_LIMIT_PER_SEC
        self._last_request_at = 0.0

    def _throttle(self):
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)

    def _request(self, method: str, path: str, retries: int = 3, **kwargs) -> dict:
        url = f"{config.NOTION_API_BASE}{path}"
        for attempt in range(retries + 1):
            self._throttle()
            resp = self.session.request(method, url, **kwargs)
            self._last_request_at = time.monotonic()

            if resp.status_code == 429 and attempt < retries:
                retry_after = float(resp.headers.get("Retry-After", "1"))
                log.warning("Notion rate-limited, retrying after %.1fs", retry_after)
                time.sleep(retry_after)
                continue

            if not resp.ok:
                raise NotionAPIError(method, path, resp.status_code, resp.text)

            return resp.json() if resp.text else {}

        raise NotionAPIError(method, path, resp.status_code, resp.text)

    def get_database(self, db_id: str) -> dict:
        return self._request("GET", f"/databases/{db_id}")

    def query_all_pages(self, db_id: str) -> list:
        pages = []
        start_cursor = None
        while True:
            payload = {}
            if start_cursor:
                payload["start_cursor"] = start_cursor
            resp = self._request("POST", f"/databases/{db_id}/query", json=payload)
            pages.extend(resp.get("results", []))
            if not resp.get("has_more"):
                break
            start_cursor = resp.get("next_cursor")
        return pages

    def update_page(self, page_id: str, properties: dict) -> dict:
        return self._request("PATCH", f"/pages/{page_id}", json={"properties": properties})

    def create_page(self, parent_db_id: str, properties: dict) -> dict:
        return self._request("POST", "/pages", json={
            "parent": {"database_id": parent_db_id},
            "properties": properties,
        })


def print_schema(client: NotionClient, db_id: str, label: str):
    db = client.get_database(db_id)
    print(f"\n=== {label} ({db_id}) ===")
    for name, prop in db.get("properties", {}).items():
        ptype = prop.get("type")
        extra = ""
        if ptype == "select":
            options = [o["name"] for o in prop.get("select", {}).get("options", [])]
            extra = f" options={options}"
        elif ptype == "status":
            options = [o["name"] for o in prop.get("status", {}).get("options", [])]
            extra = f" options={options}"
        print(f"  {name!r}: {ptype}{extra}")


# --- Property extraction helpers ---

def _plain_text_from_rich_text(rich_text_list: list) -> str:
    return "".join(rt.get("plain_text", "") for rt in rich_text_list or [])


def _extract_email(prop: dict) -> str:
    if not prop:
        return ""
    ptype = prop.get("type")
    if ptype == "email":
        return (prop.get("email") or "").strip().lower()
    if ptype == "rich_text":
        return _plain_text_from_rich_text(prop.get("rich_text")).strip().lower()
    if ptype == "title":
        return _plain_text_from_rich_text(prop.get("title")).strip().lower()
    return ""


def _extract_text(prop: dict) -> str:
    if not prop:
        return ""
    ptype = prop.get("type")
    if ptype == "title":
        return _plain_text_from_rich_text(prop.get("title"))
    if ptype == "rich_text":
        return _plain_text_from_rich_text(prop.get("rich_text"))
    if ptype == "select" and prop.get("select"):
        return prop["select"].get("name", "")
    return ""


def _extract_status(prop: dict) -> str:
    if not prop:
        return ""
    ptype = prop.get("type")
    if ptype == "select" and prop.get("select"):
        return prop["select"].get("name", "")
    if ptype == "status" and prop.get("status"):
        return prop["status"].get("name", "")
    return ""


def fetch_pipeline_leads(client: NotionClient, db_id: str = None) -> list:
    db_id = db_id or config.PIPELINE_DB_ID
    raw_pages = client.query_all_pages(db_id)
    leads = []
    for page in raw_pages:
        props = page.get("properties", {})
        leads.append(PipelineLead(
            page_id=page["id"],
            email=_extract_email(props.get(config.PIPELINE_PROP_EMAIL)),
            company_name=_extract_text(props.get(config.PIPELINE_PROP_COMPANY)),
            status=_extract_status(props.get(config.PIPELINE_PROP_STATUS)),
            action_log_text=_extract_text(props.get(config.PIPELINE_PROP_ACTION_LOG)),
        ))
    return leads


def _domain(email: str) -> str:
    return email.split("@")[-1].lower() if "@" in email else ""


PERSONAL_EMAIL_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com"}


@dataclass
class MatchResult:
    lead: PipelineLead = None
    rung: str = "no_match"  # exact_email | domain | fuzzy_company | no_match
    detail: str = ""


def match_lead(sender_email: str, sender_name: str, leads: list) -> MatchResult:
    sender_email = (sender_email or "").strip().lower()
    sender_domain = _domain(sender_email)

    # Rung 1: exact email match
    for lead in leads:
        if lead.email and lead.email == sender_email:
            return MatchResult(lead, "exact_email", f"{sender_email} == {lead.email}")

    # Rung 2: sender domain matches domain of any lead's stored email
    # (skip personal-email domains — they're not indicative of a company)
    if sender_domain and sender_domain not in PERSONAL_EMAIL_DOMAINS:
        for lead in leads:
            if lead.email and _domain(lead.email) == sender_domain:
                return MatchResult(lead, "domain", f"domain {sender_domain} matches lead {lead.email}")

    # Rung 3: fuzzy match of sender display name / domain against Company Name
    candidates = [sender_name or "", sender_domain.split(".")[0] if sender_domain else ""]
    best_lead, best_ratio = None, 0.0
    for lead in leads:
        if not lead.company_name:
            continue
        company_norm = lead.company_name.strip().lower()
        for candidate in candidates:
            candidate = candidate.strip().lower()
            if not candidate:
                continue
            ratio = difflib.SequenceMatcher(None, candidate, company_norm).ratio()
            contains_bonus = 0.15 if (candidate in company_norm or company_norm in candidate) and len(candidate) > 2 else 0
            score = min(1.0, ratio + contains_bonus)
            if score > best_ratio:
                best_ratio, best_lead = score, lead
    if best_lead and best_ratio >= config.COMPANY_FUZZY_THRESHOLD:
        return MatchResult(best_lead, "fuzzy_company",
                            f"score={best_ratio:.2f} candidates={candidates} company={best_lead.company_name!r}")

    return MatchResult(None, "no_match", f"no rung matched sender={sender_email} name={sender_name!r}")


# --- Writes ---

def format_event_log_line(event_summary: str, when: datetime = None) -> str:
    """Formats a dated note line for a reply-driven event (bounce, auto-reply,
    genuine reply, etc). This service never sends mail, so it has no
    authoritative "Sends: N" count to report — it must not fabricate one.
    It only ever appends a dated note; existing "Sends: N / Last send: ..."
    lines written by the outreach-sending tool are left untouched.

    OPEN QUESTION for the user: confirm this convention (a plain dated note
    line, distinct from the sender's own "Sends / Last send" header lines)
    is what you want here — see README "Open questions".
    """
    when = when or datetime.now()
    return f"{when.strftime('%d %b %Y')}: {event_summary}"


def append_action_log(client: NotionClient, lead: PipelineLead, new_line: str) -> bool:
    """Appends new_line to the lead's Action Log, preserving existing content.
    Idempotent: if the existing log's last non-empty line block already equals
    new_line, this is a no-op (protects against retry-induced duplicates)."""
    existing = lead.action_log_text or ""
    if existing.rstrip().endswith(new_line.strip()):
        log.info("Action Log for %s already ends with this entry, skipping append", lead.page_id)
        return False

    updated = f"{existing}\n{new_line}".strip("\n") if existing else new_line
    _write_rich_text_property(client, lead.page_id, config.PIPELINE_PROP_ACTION_LOG, updated)
    lead.action_log_text = updated
    return True


def _write_rich_text_property(client: NotionClient, page_id: str, prop_name: str, text: str):
    # Notion rich_text objects cap at 2000 chars each; chunk if needed.
    chunks = [text[i:i + 2000] for i in range(0, len(text), 2000)] or [""]
    rich_text = [{"type": "text", "text": {"content": chunk}} for chunk in chunks]
    client.update_page(page_id, {prop_name: {"rich_text": rich_text}})


def set_status(client: NotionClient, lead: PipelineLead, new_status: str):
    client.update_page(lead.page_id, {
        config.PIPELINE_PROP_STATUS: {"select": {"name": new_status}}
    })
    lead.status = new_status


def stage_bd_candidate(entry: dict, path=None):
    """Appends a BD-Database candidate to a local staging file for manual
    review/approval. Deduplicated on gmail_message_id so reruns don't
    duplicate an already-staged entry.

    NOTE: staging destination (local file vs. dedicated Notion view vs.
    Slack-digest-only) is an open question for the user to confirm — see
    README "Open questions". Local JSONL file is the default for now.
    """
    path = path or config.STAGED_BD_FILE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    existing_ids = set()
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                existing_ids.add(json.loads(line).get("gmail_message_id"))
            except json.JSONDecodeError:
                continue

    if entry.get("gmail_message_id") in existing_ids:
        log.info("BD candidate for message %s already staged, skipping", entry.get("gmail_message_id"))
        return False

    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return True


if __name__ == "__main__":
    # Build order step 4: query live schema and confirm field names/status
    # options match what the brief describes before any writes are enabled.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    client = NotionClient()
    print_schema(client, config.PIPELINE_DB_ID, "Palad BD Lead Pipeline")
    print_schema(client, config.BD_DATABASE_ID, "BD Database")
