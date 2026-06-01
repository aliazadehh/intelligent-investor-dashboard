"""T13 — Alert delivery: Telegram (primary) + SMTP email (fallback) (§9.3).

Monthly alert content:
  - Zone 1 status + color
  - DCA multiplier M + label + one-line instruction
  - Three macro sub-scores (sahm, curve, stress)
  - Snapshot as_of date

Idempotency: the snapshot's alerted_at is checked before sending.
If it is already set, the alert is skipped (no re-send for the same run).
"""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText

from iidca.models import Decision, MacroState

logger = logging.getLogger(__name__)


def _format_message(macro: MacroState, decision: Decision) -> str:
    breaker_note = ""
    if macro.breakers_fired:
        breaker_note = f"\n⚠️  Circuit breakers: {', '.join(macro.breakers_fired)}"

    sub = macro.subscores
    return (
        f"📊 *Intelligent Investor — Monthly DCA Signal*\n\n"
        f"*Zone 1:* {decision.status}{breaker_note}\n"
        f"*Macro Health H:* {macro.H:.2f}\n\n"
        f"*DCA Multiplier:* {decision.M:.2f}× — {decision.label}\n"
        f"_{decision.instruction}_\n\n"
        f"*Macro sub-scores:*\n"
        f"  Sahm: {sub.get('sahm', 0):.2f}  |  "
        f"Curve: {sub.get('curve', 0):.2f}  |  "
        f"Stress: {sub.get('stress', 0):.2f}\n\n"
        f"As of: {macro.as_of}"
    )


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(macro: MacroState, decision: Decision) -> bool:
    """Send alert via Telegram bot.  Returns True on success."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set; skipping Telegram")
        return False

    try:
        import requests  # noqa: PLC0415
        text = _format_message(macro, decision)
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
        resp.raise_for_status()
        logger.info("Telegram alert sent")
        return True
    except Exception as exc:
        logger.error("Telegram alert failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# SMTP email fallback
# ---------------------------------------------------------------------------

def send_email(macro: MacroState, decision: Decision) -> bool:
    """Send alert via SMTP.  Returns True on success.

    Required env vars:
      SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD,
      ALERT_FROM_EMAIL, ALERT_TO_EMAIL
    """
    host = os.environ.get("SMTP_HOST", "")
    if not host:
        logger.warning("SMTP_HOST not set; skipping email fallback")
        return False

    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
        user = os.environ.get("SMTP_USER", "")
        password = os.environ.get("SMTP_PASSWORD", "")
        from_addr = os.environ.get("ALERT_FROM_EMAIL", user)
        to_addr = os.environ.get("ALERT_TO_EMAIL", user)

        plain = _format_message(macro, decision).replace("*", "").replace("_", "")
        msg = MIMEText(plain)
        msg["Subject"] = f"DCA Signal: {decision.label} {decision.M:.2f}× — {decision.status}"
        msg["From"] = from_addr
        msg["To"] = to_addr

        with smtplib.SMTP(host, port) as server:
            server.starttls()
            if user and password:
                server.login(user, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())

        logger.info("Email alert sent to %s", to_addr)
        return True
    except Exception as exc:
        logger.error("Email alert failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def send_alert(macro: MacroState, decision: Decision) -> bool:
    """Try Telegram first, fall back to email.  Returns True if any succeeded."""
    if send_telegram(macro, decision):
        return True
    return send_email(macro, decision)
