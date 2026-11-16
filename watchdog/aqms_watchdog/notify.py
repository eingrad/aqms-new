"""Send alert messages by Telegram and / or email. A failed send is retried on the next ticks."""
from __future__ import annotations

import json
import logging
import smtplib
import ssl
import urllib.request
from collections import deque
from email.message import EmailMessage

from .monitor import Message

log = logging.getLogger(__name__)


class Telegram:
    """One Telegram chat (a person or a group) via a bot."""

    def __init__(self, token: str, chat_id: str, opener=urllib.request.urlopen):
        self.token = token
        self.chat_id = chat_id
        self.name = f"telegram:{chat_id}"
        self._open = opener

    def send(self, msg: Message) -> None:
        data = json.dumps({"chat_id": self.chat_id, "text": msg.body,
                           "disable_web_page_preview": True}).encode("utf-8")
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                     data=data, headers={"Content-Type": "application/json"})
        with self._open(req, timeout=15) as resp:
            reply = json.loads(resp.read().decode("utf-8"))
        if not reply.get("ok"):
            raise RuntimeError(reply.get("description", "Telegram refused the message"))


class Email:
    def __init__(self, host: str, port: int, security: str, username: str, password: str,
                 sender: str, to: list[str], smtp=smtplib):
        if security not in ("starttls", "ssl", "none"):
            raise ValueError(f"email security must be starttls, ssl or none, not {security!r}")
        self.host, self.port, self.security = host, port, security
        self.username, self.password = username, password
        self.sender, self.to = sender, to
        self.name = "email"
        self._smtp = smtp

    def send(self, msg: Message) -> None:
        m = EmailMessage()
        m["Subject"] = msg.subject
        m["From"] = self.sender
        m["To"] = ", ".join(self.to)
        m.set_content(msg.body)
        context = ssl.create_default_context()
        if self.security == "ssl":
            server = self._smtp.SMTP_SSL(self.host, self.port, timeout=20, context=context)
        else:
            server = self._smtp.SMTP(self.host, self.port, timeout=20)
        with server:
            if self.security == "starttls":
                server.starttls(context=context)
            if self.username:
                server.login(self.username, self.password)
            server.send_message(m)


class Notifier:
    """Sends every message on every channel; failures are queued and retried by `retry()`."""

    def __init__(self, channels: list, max_attempts: int = 120):
        self.channels = channels
        self.max_attempts = max_attempts            # 120 x 30 s ticks = one hour of retries
        self._outbox: deque = deque(maxlen=500)

    def send(self, messages: list[Message]) -> bool:
        ok = True
        for msg in messages:
            log.warning("ALERT %s\n%s", msg.subject, msg.body)   # always in the container log
            for channel in self.channels:
                ok &= self._try(channel, msg, 1)
        return ok

    def retry(self) -> None:
        pending = list(self._outbox)
        self._outbox.clear()
        for channel, msg, attempt in pending:
            self._try(channel, msg, attempt)

    def _try(self, channel, msg: Message, attempt: int) -> bool:
        try:
            channel.send(msg)
            log.info("sent via %s: %s", channel.name, msg.subject)
            return True
        except Exception as exc:                   # network, SMTP, Telegram API error
            if attempt < self.max_attempts:
                if attempt == 1:
                    log.warning("%s: sending %r failed (%s); retrying", channel.name,
                                msg.subject, exc)
                self._outbox.append((channel, msg, attempt + 1))
            else:
                log.error("%s: giving up on %r after %d attempts (%s)", channel.name,
                          msg.subject, attempt, exc)
            return False
