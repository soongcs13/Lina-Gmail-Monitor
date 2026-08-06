"""Slack digest notifications. One batched message per run — never per-email."""
import logging

import requests

import config

log = logging.getLogger("notify")


def build_digest_text(summary: dict) -> str:
    """summary keys: run_time (str), dry_run (bool), counts (dict),
    flagged (list of dict: kind, company, email, subject, reason),
    errors (list of str)."""
    lines = []
    mode = "DRY RUN — nothing written" if summary.get("dry_run") else "LIVE"
    lines.append(f"*Palad Lead Monitor* — {summary.get('run_time', '')} ({mode})")

    counts = summary.get("counts", {})
    if counts:
        count_str = ", ".join(f"{k}: {v}" for k, v in counts.items() if v)
        lines.append(f"Processed: {count_str or 'nothing new'}")
    else:
        lines.append("Processed: nothing new")

    flagged = summary.get("flagged", [])
    if flagged:
        lines.append("\n*Flagged for your attention:*")
        for item in flagged:
            lines.append(
                f"• [{item.get('kind', '').upper()}] {item.get('company', 'Unknown company')} "
                f"<{item.get('email', '')}> — _{item.get('subject', '')}_\n"
                f"   {item.get('reason', '')}"
            )

    errors = summary.get("errors", [])
    if errors:
        lines.append("\n*Errors during this run — needs attention:*")
        for err in errors:
            lines.append(f"• {err}")

    return "\n".join(lines)


def send_slack_digest(summary: dict, webhook_url: str = None) -> bool:
    webhook_url = webhook_url if webhook_url is not None else config.SLACK_WEBHOOK_URL
    text = build_digest_text(summary)

    if not webhook_url:
        log.warning("SLACK_WEBHOOK_URL not set — skipping Slack digest, logging it instead:\n%s", text)
        return False

    try:
        resp = requests.post(webhook_url, json={"text": text}, timeout=10)
        if not resp.ok:
            log.error("Slack webhook POST failed: %s %s", resp.status_code, resp.text[:500])
            return False
        return True
    except requests.RequestException as exc:
        log.error("Slack webhook POST raised: %s", exc)
        return False
