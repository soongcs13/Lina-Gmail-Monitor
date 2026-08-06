# Palad Lead Monitor

Headless service that polls the `lina@palad.co` Gmail inbox, classifies
replies to cold outreach, updates the Palad BD Lead Pipeline / BD Database in
Notion, and sends a batched Slack digest. Designed to run unattended via cron
on a Mac Mini.

Read-only Gmail scope — this service never sends or modifies email.

## Modules

- `config.py` — loads `.env`, holds DB IDs, property names, and constants.
- `gmail_poll.py` — OAuth flow, inbox polling, watermark state (idempotent
  re-runs — see "Idempotency" below).
- `classify.py` — deterministic regex/header pass for bounce/auto-reply
  (free, no API call), then Anthropic API for intent on genuine replies.
- `notion_write.py` — Notion REST calls, Pipeline lead matching cascade,
  read-modify-append writes, BD Database candidate staging.
- `notify.py` — one batched Slack digest per run.
- `main.py` — orchestrates a run; entry point for cron.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in NOTION_TOKEN, ANTHROPIC_API_KEY, SLACK_WEBHOOK_URL
```

Place the Gmail OAuth Desktop-app `credentials.json` in the project root (or
point `GMAIL_CREDENTIALS_PATH` at it).

## Build order — run these in sequence, inspect output before proceeding

This mirrors the intended rollout: each step is runnable standalone so you
can verify against your real inbox/Notion data before the service writes
anything.

**1. Gmail OAuth + raw polling**
```bash
python gmail_poll.py
```
Runs the OAuth browser flow on first call, persists `token.json`, and prints
raw metadata (id, from, subject, delivery-status flag) for any message newer
than the watermark. No classification, no writes.

**2. Regex classification only**
```bash
python main.py --regex-only
```
Polls Gmail and runs only the deterministic bounce/auto-reply pass, printing
the bounce/auto-reply/genuine split per message. No LLM call, no Notion
access, and the watermark is **not** advanced (safe to re-run while tuning).

**3. LLM intent classification** is wired into `classify.classify_intent()`
and exercised automatically once you move to `--dry-run` (step 4) — there's
no separate standalone step, since intent only makes sense in the context of
a matched Pipeline lead.

**4. Confirm the live Notion schema, then dry-run**
```bash
python main.py --schema-check
```
Prints every property name/type (and select/status options) for both
databases. **Compare this against `PIPELINE_PROP_*` in `.env`** — the
defaults (`Email`, `Company Name`, `Status`, `Action Log`) are best guesses
from the build brief, not confirmed against the live schema. Fix any
mismatch via the corresponding env var before proceeding.

```bash
python main.py --dry-run
```
Full pipeline: polls, classifies, matches against Pipeline, and prints
exactly what it would write to Notion — without writing. The watermark
*does* advance in dry-run mode (so a cron'd dry-run doesn't re-print the same
backlog every 15 minutes); only the actual Notion writes are suppressed. Run
this against real inbox traffic for a few days before enabling live writes.

**5. Enable live writes**
```bash
python main.py
```
Same flow as `--dry-run`, but writes to Notion for real.

**6. Slack digest** is sent at the end of every run in every mode (dry-run
included) — check `SLACK_WEBHOOK_URL` is set once you're ready for it to
actually land in Slack rather than just being logged.

**7. Cron**, once 1–5 are stable:
```cron
*/15 * * * * cd ~/palad-lead-monitor && .venv/bin/python main.py >> logs/cron.log 2>&1
```

## Idempotency

- **Gmail side**: `state/watermark.json` tracks the `internalDate` of the
  last fully-processed message plus the set of message IDs seen at that
  exact millisecond (to disambiguate ties at the boundary). The watermark
  only advances past a message once it has been fully handled; a message
  that throws partway through processing is retried on the next run instead
  of being skipped.
- **Notion side**: `append_action_log()` is a no-op if the Action Log
  already ends with the line being appended, so a retried write after a
  partial failure won't duplicate the entry. `stage_bd_candidate()`
  deduplicates on the Gmail message ID.

## Classification & matching

Deterministic bounce/auto-reply detection and the lead-matching cascade
(exact email → sender domain → fuzzy company-name → no match/Irrelevant) are
implemented exactly as specified in the build brief, with every match
decision logged (`match_rung` + detail) for audit. Genuine replies with no
Pipeline match are dropped as Irrelevant and logged, never written.

Per the explicit design decision in the brief: bounces and auto-replies
write to the Pipeline DB automatically; anything that would move a lead into
the **BD Database** (an `interested` classification) is staged to
`state/staged_bd_migrations.jsonl` for manual review rather than
auto-created — see "Open questions" below for where that staging should
actually live long-term.

## Open questions — flagged per the build brief, not assumed

1. **Pipeline property names.** `PIPELINE_PROP_EMAIL` / `_COMPANY` /
   `_STATUS` / `_ACTION_LOG` in `.env` default to `Email`, `Company Name`,
   `Status`, `Action Log` per the brief's description. Run
   `python main.py --schema-check` against the live database and confirm —
   flag any mismatch and I'll update the defaults.
2. **Polling interval.** Cron example above uses 15 minutes as instructed.
   Gmail API quota (250 quota units/user/second; `messages.list` = 5 units,
   `messages.get` = 5 units) comfortably supports this even with a full
   inbox re-list each run — not a concern at this volume. Flag if you'd
   rather poll less often.
3. **Where staged BD Database candidates should live.** Currently a local
   JSONL file (`state/staged_bd_migrations.jsonl`) plus a mention in each
   Slack digest. Alternatives: a dedicated Notion database/view you triage
   from directly, or Slack-digest-only with no local file. Local file was
   chosen as the simplest default that doesn't touch the BD Database
   automatically — confirm or redirect.
4. **Action Log line format for reply-driven events.** The brief's
   `Sends: N / Last send: DD Mon YYYY` template describes entries written by
   whatever tool sends the outreach — this service never sends anything, so
   it has no authoritative send count and does not fabricate one. Instead it
   appends a plain dated note line (e.g. `06 Aug 2026: Bounce detected from
   inbound message (...)`) below existing content, leaving prior
   `Sends / Last send` lines untouched. Confirm this convention or specify
   the exact format you want for monitor-generated lines.

## Non-negotiables checklist

- Idempotent re-runs (watermark + no-op-on-duplicate writes) — done.
- `--dry-run` flag, full classification without writes — done.
- Fails loudly: Notion/Gmail/Anthropic errors are logged, added to
  `summary["errors"]`, surfaced in the Slack digest, and produce a non-zero
  exit code for cron alerting. No bare `except: pass` anywhere.
- Read-only Gmail scope only; no send capability exists in this codebase.
- Notion writes throttled to ≤3 req/s in `NotionClient`.
