"""Orchestrates one run of the Palad lead monitor: poll Gmail, classify,
match against the Pipeline DB, write (or dry-run print) the resulting
Notion updates, and send one batched Slack digest.

Usage:
    python main.py --dry-run          # classify + match, print intended writes, write nothing
    python main.py                    # live run, writes to Notion
    python main.py --regex-only       # build-order step 2: regex classification only, no LLM, no writes
    python main.py --schema-check     # build-order step 4: print live Notion schema, no polling
"""
import argparse
import logging
import logging.handlers
import sys
from datetime import datetime, timezone

import anthropic

import classify
import config
import gmail_poll
import notify
import notion_write

log = logging.getLogger("main")


def setup_logging():
    config.LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        config.LOG_FILE_PATH, maxBytes=5_000_000, backupCount=5
    )
    stream = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    handler.setFormatter(fmt)
    stream.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(stream)


def run(dry_run: bool) -> int:
    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    summary = {"run_time": run_time, "dry_run": dry_run,
               "counts": {"bounce": 0, "auto_reply": 0, "interested": 0, "declined": 0,
                          "unsubscribe": 0, "unclear": 0, "irrelevant": 0, "bd_reengagement": 0},
               "flagged": [], "errors": []}

    try:
        service = gmail_poll.get_gmail_service()
    except Exception as exc:
        log.critical("Gmail auth failed: %s", exc)
        summary["errors"].append(f"Gmail auth failed: {exc}")
        notify.send_slack_digest(summary)
        return 1

    state = gmail_poll.load_state()
    try:
        messages = gmail_poll.poll_new_messages(service, state)
    except Exception as exc:
        log.critical("Gmail poll failed: %s", exc)
        summary["errors"].append(f"Gmail poll failed: {exc}")
        notify.send_slack_digest(summary)
        return 1

    log.info("Polled %d new message(s)", len(messages))
    if not messages:
        notify.send_slack_digest(summary)
        return 0

    notion_client = None
    leads = []
    bd_leads = []
    try:
        notion_client = notion_write.NotionClient()
        leads = notion_write.fetch_pipeline_leads(notion_client)
        log.info("Fetched %d leads from Pipeline DB", len(leads))
        bd_leads = notion_write.fetch_bd_leads(notion_client)
        log.info("Fetched %d contacts from BD Database", len(bd_leads))
    except Exception as exc:
        log.critical("Failed to fetch leads from Notion: %s", exc)
        summary["errors"].append(f"Failed to fetch leads from Notion: {exc}")
        notify.send_slack_digest(summary)
        return 1

    anthropic_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY) if config.ANTHROPIC_API_KEY else None

    for msg in messages:
        try:
            _process_message(msg, leads, bd_leads, notion_client, anthropic_client, dry_run, summary)
        except Exception as exc:
            log.exception("Unhandled error processing message %s", msg.id)
            summary["errors"].append(f"Failed processing message {msg.id} ({msg.subject!r}): {exc}")
            # Do not advance the watermark past a message we failed to fully
            # process — it will be retried on the next run.
            continue
        state = gmail_poll.advance_state(state, msg)
        gmail_poll.save_state(state)

    notify.send_slack_digest(summary)
    return 1 if summary["errors"] else 0


def _process_message(msg, leads, bd_leads, notion_client, anthropic_client, dry_run, summary):
    deterministic = classify.classify_deterministic(msg)

    if deterministic == "bounce":
        summary["counts"]["bounce"] += 1
        # The bounce's sender is always mailer-daemon — match against the
        # original recipient address extracted from the bounce body instead.
        if not msg.bounced_recipient:
            log.warning("BOUNCE msg=%s has no extractable recipient address — flagging for manual review", msg.id)
            summary["errors"].append(
                f"Bounce message {msg.id} ({msg.subject!r}): could not extract the failed recipient address, "
                "so it could not be matched to a Pipeline lead. Needs manual review."
            )
            return
        match = notion_write.match_lead(msg.bounced_recipient, "", leads, subject=msg.subject)
        log.info("BOUNCE msg=%s bounced_recipient=%s match_rung=%s detail=%s",
                  msg.id, msg.bounced_recipient, match.rung, match.detail)
        if match.lead:
            line = notion_write.format_event_log_line(
                f"Bounce detected from inbound message ({msg.subject!r})"
            )
            if dry_run:
                log.info("[DRY RUN] Would set status=Bounced and append action log for %s", match.lead.page_id)
            else:
                notion_write.set_status(notion_client, match.lead, config.STATUS_BOUNCED)
                notion_write.append_action_log(notion_client, match.lead, line)
        return

    if deterministic == "auto_reply":
        summary["counts"]["auto_reply"] += 1
        match = notion_write.match_lead(msg.from_email, msg.from_name, leads, subject=msg.subject)
        log.info("AUTO_REPLY msg=%s match_rung=%s detail=%s", msg.id, match.rung, match.detail)
        if match.lead:
            note = notion_write.format_event_log_line(
                f"Auto-reply / OOO received ({msg.subject!r}) — not treated as engagement, no status change."
            )
            if dry_run:
                log.info("[DRY RUN] Would append action-log note (no status change) for %s", match.lead.page_id)
            else:
                notion_write.append_action_log(notion_client, match.lead, note)
        return

    # Genuine reply — but first exclude Palad's own team members. Their
    # replies land in this inbox via CC on lead threads; they are never
    # inbound lead replies and must not be matched/flagged as one.
    sender_domain = msg.from_email.split("@")[-1].lower() if "@" in msg.from_email else ""
    if sender_domain == config.PALAD_INTERNAL_DOMAIN:
        summary["counts"]["irrelevant"] += 1
        log.info("INTERNAL msg=%s sender=%s is a Palad-internal address, not a lead reply — classified Irrelevant",
                  msg.id, msg.from_email)
        return

    match = notion_write.match_lead(msg.from_email, msg.from_name, leads, subject=msg.subject)
    log.info("GENUINE msg=%s match_rung=%s detail=%s", msg.id, match.rung, match.detail)
    if not match.lead:
        # No Pipeline match — check whether this is a re-engagement from a
        # lead already migrated to BD Database (human-owned). If so, don't
        # write anything except flipping Follow up? to Yes; never touch
        # Pipeline or create a duplicate BD Database row.
        bd_match = notion_write.match_bd_lead(msg.from_email, msg.from_name, bd_leads, subject=msg.subject)
        log.info("BD_DATABASE_CHECK msg=%s match_rung=%s detail=%s", msg.id, bd_match.rung, bd_match.detail)
        if bd_match.lead:
            summary["counts"]["bd_reengagement"] += 1
            if dry_run:
                log.info("[DRY RUN] Would set Follow up?=Yes on existing BD Database entry %s", bd_match.lead.page_id)
            else:
                notion_write.set_bd_follow_up(notion_client, bd_match.lead)
            summary["flagged"].append({
                "kind": "bd_reengagement", "company": bd_match.lead.name, "email": msg.from_email,
                "subject": msg.subject, "reason": "Existing BD Database contact sent a new message — Follow up? set to Yes.",
            })
            return

        summary["counts"]["irrelevant"] += 1
        log.info("No Pipeline or BD Database match for message %s from %s — classified Irrelevant, dropped",
                  msg.id, msg.from_email)
        return

    if anthropic_client is None:
        intent_result = {"intent": "unclear", "confidence": "low",
                          "reasoning": "ANTHROPIC_API_KEY not configured", "error": True}
    else:
        intent_result = classify.classify_intent(msg, anthropic_client)

    intent = intent_result["intent"]
    summary["counts"][intent] += 1
    if intent_result.get("error"):
        summary["errors"].append(f"LLM classification issue for message {msg.id}: {intent_result['reasoning']}")

    lead = match.lead
    if intent == "interested":
        line = notion_write.format_event_log_line(
            f"Genuine reply — interested ({msg.subject!r}). Created in BD Database."
        )
        bd_notes = (
            f"Auto-created from inbound reply on {datetime.now().strftime('%d %b %Y')}.\n"
            f"Subject: {msg.subject}\n"
            f"From: {msg.raw_from}\n"
            f"Reasoning: {intent_result.get('reasoning', '')}\n"
            f"Pipeline record: {lead.page_id}"
        )
        bd_name = lead.company_name or msg.from_name or msg.from_email
        if dry_run:
            log.info("[DRY RUN] Would set status=Replied, append action log, and create BD Database entry for %s", lead.page_id)
        else:
            notion_write.set_status(notion_client, lead, config.STATUS_REPLIED)
            notion_write.append_action_log(notion_client, lead, line)
            notion_write.create_bd_candidate(notion_client, lead, msg.id, msg.from_email, bd_name, bd_notes)
        summary["flagged"].append({
            "kind": "interested", "company": lead.company_name, "email": msg.from_email,
            "subject": msg.subject, "reason": "Created in BD Database (Status = Not started).",
        })

    elif intent == "declined":
        reason = intent_result.get("reasoning", "")
        line = notion_write.format_event_log_line(f"Declined ({msg.subject!r}): {reason}")
        if dry_run:
            log.info("[DRY RUN] Would set status=Closed-Lost and append action log for %s", lead.page_id)
        else:
            notion_write.set_status(notion_client, lead, config.STATUS_CLOSED_LOST)
            notion_write.append_action_log(notion_client, lead, line)
        summary["flagged"].append({
            "kind": "declined", "company": lead.company_name, "email": msg.from_email,
            "subject": msg.subject, "reason": reason,
        })

    elif intent == "unsubscribe":
        line = notion_write.format_event_log_line(f"Unsubscribe request ({msg.subject!r})")
        if dry_run:
            log.info("[DRY RUN] Would set status=Do Not Contact and append action log for %s", lead.page_id)
        else:
            notion_write.set_status(notion_client, lead, config.STATUS_DNC)
            notion_write.append_action_log(notion_client, lead, line)
        summary["flagged"].append({
            "kind": "unsubscribe", "company": lead.company_name, "email": msg.from_email,
            "subject": msg.subject, "reason": "Marked Do Not Contact.",
        })

    else:  # unclear
        line = notion_write.format_event_log_line(
            f"Reply intent unclear ({msg.subject!r}) — flagged for manual review: {intent_result.get('reasoning', '')}"
        )
        if dry_run:
            log.info("[DRY RUN] Would append action-log manual-review note (no status change) for %s", lead.page_id)
        else:
            notion_write.append_action_log(notion_client, lead, line)
        summary["flagged"].append({
            "kind": "unclear", "company": lead.company_name, "email": msg.from_email,
            "subject": msg.subject, "reason": intent_result.get("reasoning", "Could not confidently classify intent."),
        })


def schema_check():
    client = notion_write.NotionClient()
    notion_write.print_schema(client, config.PIPELINE_DB_ID, "Palad BD Lead Pipeline")
    notion_write.print_schema(client, config.BD_DATABASE_ID, "BD Database")


def regex_only():
    service = gmail_poll.get_gmail_service()
    state = gmail_poll.load_state()
    messages = gmail_poll.poll_new_messages(service, state)
    counts = {"bounce": 0, "auto_reply": 0, "genuine": 0}
    for m in messages:
        result = classify.classify_deterministic(m) or "genuine"
        counts[result] += 1
        print(f"[{result:10s}] {m.raw_from!r} | {m.subject!r}")
    print("\n--- Summary (regex-only, watermark NOT advanced) ---")
    for k, v in counts.items():
        print(f"{k}: {v}")


def main():
    parser = argparse.ArgumentParser(description="Palad lead monitor")
    parser.add_argument("--dry-run", action="store_true",
                         help="Classify and match, print intended Notion writes, write nothing.")
    parser.add_argument("--regex-only", action="store_true",
                         help="Build-order step 2: regex classification only, no LLM, no Notion, no watermark advance.")
    parser.add_argument("--schema-check", action="store_true",
                         help="Build-order step 4: print live Notion database schemas and exit.")
    args = parser.parse_args()

    setup_logging()

    if args.schema_check:
        schema_check()
        return 0
    if args.regex_only:
        regex_only()
        return 0

    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
