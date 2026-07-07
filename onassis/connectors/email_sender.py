"""Email connector — send the marketing kit's newsletter via SMTP.

Gated + injectable like the other connectors: without ``SMTP_HOST`` (+ from/to
addresses) it is a safe no-op. The transport (an object exposing ``send``) is
injectable so tests never open a socket; the default uses stdlib ``smtplib``.
"""

from __future__ import annotations

from typing import Any

from onassis.config import Config
from onassis.logger import get_logger

log = get_logger(__name__)


class EmailSender:
    name = "email"

    def __init__(self, config: Config, transport: Any | None = None) -> None:
        self.config = config
        self.cfg = config.email or {}
        self._transport = transport

    @property
    def can_publish(self) -> bool:
        return self._transport is not None or bool(
            self.cfg.get("smtp_host") and self.cfg.get("from_address")
            and self.cfg.get("to_address"))

    def _t(self) -> Any:
        if self._transport is None:
            self._transport = SMTPTransport(
                host=self.cfg.get("smtp_host"), port=int(self.cfg.get("smtp_port", 587)),
                user=self.cfg.get("smtp_user"), password=self.cfg.get("smtp_password"),
                use_tls=bool(self.cfg.get("use_tls", True)))
        return self._transport

    def send(self, subject: str, body: str, *, to: str | None = None) -> dict[str, Any]:
        if not self.can_publish:
            return {"ok": False, "skipped": True, "reason": "Email not configured."}
        recipient = to or self.cfg.get("to_address")
        sender = self.cfg.get("from_address")
        ref = self._t().send(sender=sender, to=recipient, subject=subject, body=body)
        return {"ok": True, "ref": str(ref or recipient)}

    def test_connection(self) -> dict[str, Any]:
        """Connect + authenticate to the SMTP server without sending anything."""
        if not self.can_publish:
            return {"ok": False, "configured": False,
                    "detail": "Set SMTP_HOST + EMAIL_FROM + EMAIL_TO."}
        try:
            self._t().test()
            return {"ok": True, "configured": True,
                    "detail": f"SMTP {self.cfg.get('smtp_host')} reachable"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "configured": True, "detail": str(exc)}

    def send_test(self) -> dict[str, Any]:
        """Send a validation email to the configured recipient."""
        return self.send(
            "ONASSIS test email ✓",
            "This is a test email from your ONASSIS Operations Centre.\n\n"
            "If you can read this, SMTP is configured correctly.")


class SMTPTransport:
    """Sends a plain-text email via SMTP. Injectable for tests."""

    def __init__(self, host: str | None, port: int = 587, user: str | None = None,
                 password: str | None = None, use_tls: bool = True,
                 timeout: float = 30.0) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.use_tls = use_tls
        self.timeout = timeout

    def send(self, *, sender: str, to: str, subject: str, body: str) -> str:
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            if self.use_tls:
                smtp.starttls()
            if self.user:
                smtp.login(self.user, self.password or "")
            smtp.send_message(msg)
        return msg["Message-ID"] or to

    def test(self) -> bool:
        """Open the SMTP connection (+ TLS/login) and hang up — no message sent."""
        import smtplib

        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            if self.use_tls:
                smtp.starttls()
            if self.user:
                smtp.login(self.user, self.password or "")
            smtp.noop()
        return True
