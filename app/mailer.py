"""Sending email (sign-up checks, password resets, approvals) through any SMTP service.

Settings (stack.env): SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM, SMTP_SECURITY (starttls | ssl | none).
Without SMTP_HOST, emails are written to the log instead, so you can still test.
"""
import os
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .db import site_setting

sent: list[EmailMessage] = []  # the last few messages, for tests


def configured() -> bool:
    return bool(os.getenv("SMTP_HOST"))


def _deliver(msg: EmailMessage) -> None:
    host = os.getenv("SMTP_HOST", "")
    if not host:
        print(f"[payday] email to {msg['To']} (no SMTP_HOST set, not sent): {msg['Subject']}\n{msg.get_content()}", flush=True)
        return
    port = int(os.getenv("SMTP_PORT", "587"))
    security = os.getenv("SMTP_SECURITY", "starttls").lower()
    ctx = ssl.create_default_context()
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            if security == "starttls":
                server.starttls(context=ctx)
        with server:
            if os.getenv("SMTP_USER"):
                server.login(os.environ["SMTP_USER"], os.getenv("SMTP_PASSWORD", ""))
            server.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        print(f"[payday] email to {msg['To']} failed: {type(e).__name__}", flush=True)


def send(to: str, subject: str, body: str) -> None:
    """Send in the background so pages don't wait on the mail server."""
    name = site_setting("business_name", "Payday")
    sender = os.getenv("SMTP_FROM") or site_setting("contact_email") or "payday@localhost"
    msg = EmailMessage()
    msg["From"] = formataddr((name, sender))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)
    msg.set_content(body + f"\n\n{name}\n")
    sent.append(msg)
    del sent[:-20]
    threading.Thread(target=_deliver, args=(msg,), daemon=True).start()
