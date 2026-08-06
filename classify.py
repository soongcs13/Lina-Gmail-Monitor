"""Message classification: deterministic regex/header pass first (free), then
Anthropic API for intent on whatever survives as a genuine reply.

No LLM call is ever spent on a bounce or auto-reply.
"""
import json
import logging
import re

import anthropic

import config
from gmail_poll import GmailMessage

log = logging.getLogger("classify")

_BOUNCE_SENDER_RE = re.compile(config.BOUNCE_SENDER_PATTERN, re.I)
_BOUNCE_SUBJECT_RE = re.compile(config.BOUNCE_SUBJECT_PATTERN, re.I)
_AUTO_REPLY_SUBJECT_RE = re.compile(config.AUTO_REPLY_SUBJECT_PATTERN, re.I)
_AUTO_SUBMITTED_RE = re.compile(config.AUTO_SUBMITTED_PATTERN, re.I)
_PRECEDENCE_BULK_RE = re.compile(config.PRECEDENCE_BULK_PATTERN, re.I)

VALID_INTENTS = {"interested", "declined", "unsubscribe", "unclear"}

INTENT_SYSTEM_PROMPT = """You classify inbound email replies to B2B cold outreach sent by Palad, a company selling equipment-rental / insurance-adjacent services to equipment rental, clinic, event rental, and car rental businesses in Singapore.

Classify the reply's intent into exactly one of:
- "interested": wants to talk, asks a question, requests more info, forwards to a colleague, asks for a call/demo.
- "declined": not interested, no budget, wrong fit, already has a provider, explicitly says no thanks.
- "unsubscribe": explicit request to stop contacting them (stronger than a plain decline — e.g. "remove me", "stop emailing", "unsubscribe").
- "unclear": you cannot confidently tell from the text which of the above applies.

Respond with ONLY a JSON object, no prose, no markdown fences, in exactly this shape:
{"intent": "<one of interested|declined|unsubscribe|unclear>", "confidence": "<high|medium|low>", "reasoning": "<one sentence>"}

If you are not confident, choose "unclear" rather than guessing — a false positive on "interested" or "unsubscribe" has real business cost."""


def has_delivery_status_indicator(msg: GmailMessage) -> bool:
    return msg.has_delivery_status_part


def classify_deterministic(msg: GmailMessage) -> str | None:
    """Returns 'bounce', 'auto_reply', or None (genuine — needs LLM intent)."""
    if _BOUNCE_SENDER_RE.search(msg.from_email) or _BOUNCE_SUBJECT_RE.search(msg.subject):
        return "bounce"
    if has_delivery_status_indicator(msg):
        return "bounce"

    if _AUTO_REPLY_SUBJECT_RE.search(msg.subject):
        return "auto_reply"
    for header_name in config.AUTO_REPLY_HEADER_NAMES:
        if msg.headers.get(header_name):
            return "auto_reply"
    auto_submitted = msg.headers.get("auto-submitted", "")
    if _AUTO_SUBMITTED_RE.search(auto_submitted):
        return "auto_reply"
    precedence = msg.headers.get("precedence", "")
    if _PRECEDENCE_BULK_RE.search(precedence):
        return "auto_reply"

    return None


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    return text


def classify_intent(msg: GmailMessage, client: anthropic.Anthropic | None = None) -> dict:
    """Calls the Anthropic API to classify intent for a genuine reply.

    Returns {"intent": ..., "confidence": ..., "reasoning": ..., "error": bool}.
    Never raises — on any failure to get a parseable result, returns
    intent="unclear" with error=True so it surfaces in the digest rather than
    silently defaulting.
    """
    client = client or anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    body_excerpt = (msg.body_text or "").strip()[:4000]
    user_content = f"Subject: {msg.subject}\n\nBody:\n{body_excerpt}"

    try:
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=300,
            system=INTENT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        raw_text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        parsed = json.loads(_strip_code_fences(raw_text))
        intent = parsed.get("intent")
        if intent not in VALID_INTENTS:
            log.error("LLM returned invalid intent %r for message %s, treating as unclear", intent, msg.id)
            return {"intent": "unclear", "confidence": "low",
                     "reasoning": f"Invalid intent from model: {intent!r}", "error": True}
        parsed.setdefault("confidence", "low")
        parsed.setdefault("reasoning", "")
        parsed["error"] = False
        return parsed
    except json.JSONDecodeError as exc:
        log.error("LLM response unparseable for message %s: %s", msg.id, exc)
        return {"intent": "unclear", "confidence": "low",
                 "reasoning": "LLM response was not valid JSON", "error": True}
    except Exception as exc:
        log.error("Anthropic API call failed for message %s: %s", msg.id, exc)
        return {"intent": "unclear", "confidence": "low",
                 "reasoning": f"Anthropic API error: {exc}", "error": True}


if __name__ == "__main__":
    # Build order step 2: regex layer only, run against real inbox history,
    # show the bounce/auto-reply/genuine split. No LLM calls.
    import argparse

    from gmail_poll import get_gmail_service, load_state, poll_new_messages

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.parse_args()

    svc = get_gmail_service()
    state = load_state()
    messages = poll_new_messages(svc, state)

    counts = {"bounce": 0, "auto_reply": 0, "genuine": 0}
    for m in messages:
        result = classify_deterministic(m) or "genuine"
        counts[result] += 1
        print(f"[{result:10s}] {m.raw_from!r} | {m.subject!r}")

    print("\n--- Summary ---")
    for k, v in counts.items():
        print(f"{k}: {v}")
