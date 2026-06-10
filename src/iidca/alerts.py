"""Alert delivery: Telegram (primary) + SMTP email (fallback).

Cycle alert content:
  - Global macro status + H + any breakers
  - Per asset: DCA multiplier M + label + trend-residual Z
  - Snapshot as_of date

Idempotency: the caller checks/sets alerted_at on the snapshot rows before
re-sending for the same run.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from iidca.run import CycleResult

logger = logging.getLogger(__name__)


def _format_message(result: CycleResult) -> str:
    macro = result.macro
    any_decision = next(iter(result.assets.values())).decision if result.assets else None
    status = any_decision.status if any_decision else macro.regime

    fired = macro.breakers_fired + macro.soft_breakers_fired
    breaker_note = f"\n⚠️  Breakers: {', '.join(fired)}" if fired else ""

    sub = macro.subscores
    lines = [
        "📊 *Intelligent Investor — DCA Signal*",
        "",
        f"*Macro:* {status}{breaker_note}",
        f"*Health H:* {macro.H:.2f}  "
        f"(Sahm {sub.get('sahm', 0):.2f} · Curve {sub.get('curve', 0):.2f} · "
        f"Stress {sub.get('stress', 0):.2f})",
        "",
    ]
    for symbol, res in result.assets.items():
        t, d = res.tech, res.decision
        flag = "" if t.data_ok else "  ⚠ fail-safe"
        lines.append(f"*{symbol}:* {d.M:.2f}× — {d.label}  (Z {t.z:+.2f}){flag}")
        lines.append(f"_{d.instruction}_")
    lines.append("")
    lines.append(f"As of: {macro.as_of}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(result: CycleResult) -> bool:
    """Send alert via Telegram bot.  Returns True on success."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set; skipping Telegram")
        return False

    try:
        import requests  # noqa: PLC0415
        text = _format_message(result)
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

def send_email(result: CycleResult) -> bool:
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

        plain = _format_message(result).replace("*", "").replace("_", "")
        msg = MIMEText(plain)
        macro = result.macro
        msg["Subject"] = f"DCA Signal — {macro.regime}, {len(result.assets)} asset(s)"
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

def send_alert(result: CycleResult) -> bool:
    """Try Telegram first, fall back to email.  Returns True if any succeeded."""
    if send_telegram(result):
        return True
    return send_email(result)
