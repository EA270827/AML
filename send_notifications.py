"""
AML Notification Sender  —  SEPARATE, GATED, DISABLED BY DEFAULT
================================================================

Delivers notifications STAGED in `aml_notifications` (status='pending').
Open internal SMTP relay (no login), matching the ATM-alert setup.

v2.13 model: one email per RM, with personnel (segment manager / management)
in CC — no separate management email.

** WILL NOT SEND unless BOTH are true: **
   1. AML_NOTIFY_ENABLED=true   (master switch)
   2. you pass  --send

Default is a DRY RUN (prints, sends nothing).

TEST-RECIPIENT OVERRIDE (for safe testing before going live to RMs):
   AML_NOTIFY_OVERRIDE_TO = you@bank.com[,other@bank.com]
   With an override set, mail goes to the OVERRIDE address, not real RMs.
   AML_NOTIFY_OVERRIDE_MODE controls CC behaviour:
      'redirect' (default, safest) — ALL mail to override only; real To/CC shown
                 in the body header but receive nothing.
      'keepcc'   — To = override, but real CC addresses ARE included, so you can
                 verify CC delivery works end-to-end.

Usage:
    python send_notifications.py                 # dry run
    python send_notifications.py --send          # sends if AML_NOTIFY_ENABLED=true
                                                 # (to override addr if set, else real)
    python send_notifications.py --selftest      # send ONE dummy email to confirm the
                                                 # SMTP path works, regardless of pending.
                                                 # Goes only to AML_NOTIFY_OVERRIDE_TO
                                                 # or --to=you@bank.com — never to an RM.
"""
from __future__ import annotations

import os
import sys
import smtplib
import logging
from email.mime.text import MIMEText
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import create_engine, text, URL

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s  %(message)s")
log = logging.getLogger("aml_notify")

SEND_REQUESTED = "--send" in sys.argv
NOTIFY_ENABLED = os.environ.get("AML_NOTIFY_ENABLED", "").lower() == "true"
TEST_MODE = not (SEND_REQUESTED and NOTIFY_ENABLED)

SMTP_SERVER = os.environ.get("AML_SMTP_HOST")
SMTP_PORT = int(os.environ.get("AML_SMTP_PORT", "25"))
EMAIL_FROM = os.environ.get("AML_NOTIFY_FROM")

OVERRIDE_TO = [e.strip() for e in
               os.environ.get("AML_NOTIFY_OVERRIDE_TO", "").split(",") if e.strip()]
OVERRIDE_MODE = os.environ.get("AML_NOTIFY_OVERRIDE_MODE", "redirect").lower()


def _engine():
    url = os.environ.get("AML_WRITE_URL")
    if not url:
        url = URL.create(
            "mssql+pyodbc",
            username=os.environ["AML_WRITE_USER"], password=os.environ["AML_WRITE_PWD"],
            host=os.environ["AML_WRITE_HOST"], port=int(os.environ.get("AML_WRITE_PORT", "1433")),
            database=os.environ["AML_WRITE_DB"],
            query={"driver": os.environ.get("AML_ODBC_DRIVER", "ODBC Driver 17 for SQL Server")})
    return create_engine(url)


def _split(val):
    if not val or (isinstance(val, float) and pd.isna(val)):
        return []
    return [a.strip() for a in str(val).split(",") if a.strip()]


def _resolve_addrs(row):
    """Return (to_list, cc_list, note) after applying any test override.
    note describes the real intended routing, shown in the body during testing."""
    real_to = _split(row.get("recipient_email"))
    real_cc = _split(row.get("cc_email"))

    if not OVERRIDE_TO:
        return real_to, real_cc, None  # live routing

    note = (f"[TEST OVERRIDE active] real To: {', '.join(real_to) or '(none)'}; "
            f"real CC: {', '.join(real_cc) or '(none)'}")
    if OVERRIDE_MODE == "keepcc":
        return OVERRIDE_TO, real_cc, note          # test addr + real CCs
    return OVERRIDE_TO, [], note                     # redirect: override only


def send_one(row, eng):
    to_list, cc_list, note = _resolve_addrs(row)
    body = row["body"]
    if note:
        body = note + "\n\n" + body

    if TEST_MODE:
        print("\n" + "=" * 60)
        print(f"[TEST MODE] WOULD SEND -> To: {', '.join(to_list) or '(none)'}"
              f" | CC: {', '.join(cc_list) or '(none)'}")
        print(f"SUBJECT: {row['subject']}")
        print(body)
        return False

    if not to_list:
        log.warning("Notification %s has no recipient — skipping", row["notification_id"])
        return False

    msg = MIMEText(body)
    msg["Subject"] = row["subject"]
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)

    all_rcpts = to_list + cc_list
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:   # open relay, no login
        server.sendmail(EMAIL_FROM, all_rcpts, msg.as_string())

    with eng.begin() as cx:
        cx.execute(text("UPDATE aml_notifications SET status='sent', sent_at=:t "
                        "WHERE notification_id=:i"),
                   {"t": datetime.now(timezone.utc).replace(tzinfo=None),
                    "i": int(row["notification_id"])})
    log.info("Sent notification %s -> To:%s CC:%s", row["notification_id"],
             ", ".join(to_list), ", ".join(cc_list) or "-")
    return True


def self_test():
    """Send a single dummy email through the real SMTP path to confirm the
    plumbing works — independent of whether any notifications are pending.

    Goes ONLY to AML_NOTIFY_OVERRIDE_TO (or --to=addr), so it can never reach a
    real RM. Requires an explicit test recipient; refuses to run without one."""
    to_list = OVERRIDE_TO or _cli_to()
    if not to_list:
        log.error("Self-test needs a test recipient. Set AML_NOTIFY_OVERRIDE_TO "
                  "or pass --to=you@bank.com")
        return
    if not SMTP_SERVER or not EMAIL_FROM:
        log.error("Self-test needs AML_SMTP_HOST and AML_NOTIFY_FROM set.")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    body = ("AML notification self-test.\n\n"
            f"If you received this, the email path works:\n"
            f"  relay : {SMTP_SERVER}:{SMTP_PORT}\n"
            f"  from  : {EMAIL_FROM}\n"
            f"  to    : {', '.join(to_list)}\n"
            f"  sent  : {stamp}\n\n"
            "This is a plumbing test only — no screening data included.")
    msg = MIMEText(body)
    msg["Subject"] = "[Self-test] AML notification email path"
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(to_list)

    log.info("Self-test: sending a dummy email -> %s via %s:%s",
             ", ".join(to_list), SMTP_SERVER, SMTP_PORT)
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            server.sendmail(EMAIL_FROM, to_list, msg.as_string())
        log.info("Self-test SENT — check the inbox of %s", ", ".join(to_list))
    except Exception as exc:  # noqa: BLE001 — surface the real SMTP error
        log.error("Self-test FAILED to send: %s", exc)
        log.error("Check AML_SMTP_HOST/PORT, that this host may relay, and "
                  "that AML_NOTIFY_FROM is an accepted sender.")


def _cli_to():
    for a in sys.argv:
        if a.startswith("--to="):
            return [e.strip() for e in a.split("=", 1)[1].split(",") if e.strip()]
    return []


def main():
    if "--selftest" in sys.argv:
        self_test()
        return

    eng = _engine()
    today = datetime.now(timezone.utc).date()
    pending = pd.read_sql(
        "SELECT * FROM aml_notifications WHERE status='pending' AND run_date=?",
        eng, params=(today,))

    if pending.empty:
        log.info("No pending notifications for %s", today)
        return

    log.info("%s pending notification(s) for %s", len(pending), today)
    if OVERRIDE_TO:
        log.warning("TEST OVERRIDE active (mode=%s) -> all mail to: %s",
                    OVERRIDE_MODE, ", ".join(OVERRIDE_TO))
    if TEST_MODE:
        why = "no --send flag" if not SEND_REQUESTED else "AML_NOTIFY_ENABLED is not 'true'"
        log.warning("DRY RUN (%s) — printing only, nothing sent.", why)

    sent = sum(1 for _, row in pending.iterrows() if send_one(row, eng))

    if TEST_MODE:
        log.warning("Dry run complete — nothing sent.")
    else:
        log.info("Send complete — %s message(s) sent%s.", sent,
                 " (TEST OVERRIDE)" if OVERRIDE_TO else "")


if __name__ == "__main__":
    main()