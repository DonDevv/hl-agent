"""Web Push to the phones that installed the PWA.

The browser hands us a ``PushSubscription`` (endpoint + two keys); we keep them in
``<dir>/subscriptions.json`` and sign every message with a VAPID key pair that lives in
``<dir>/vapid.pem``. The key is generated on first use, on the machine that serves the
dashboard, and is never shown anywhere — the page only ever sees the public half.

Sending is delegated to ``pywebpush`` (optional ``[web]`` dependency); tests inject a fake
sender. A push service answering 404/410 means the phone dropped the subscription, so it is
forgotten.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_SUBJECT = "https://github.com/DonDevv/hl-agent"  # VAPID "sub": who to contact
MAX_SUBSCRIPTIONS = 20
TTL_S = 6 * 3600  # how long the push service keeps an undelivered message


class SubscriptionGone(Exception):  # noqa: N818 — reads as a state, not an error
    """The push service no longer knows this endpoint."""


Sender = Callable[[dict[str, Any], str], None]
"""``(subscription, json_payload)``; raises ``SubscriptionGone`` for a dead endpoint."""


@dataclass(frozen=True, slots=True)
class Notification:
    title: str
    body: str
    url: str = "/"
    tag: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {"title": self.title, "body": self.body, "url": self.url, "tag": self.tag},
            ensure_ascii=False,
        )


def _valid(sub: Any) -> dict[str, Any] | None:
    if not isinstance(sub, dict):
        return None
    endpoint, keys = sub.get("endpoint"), sub.get("keys")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        return None
    if not isinstance(keys, dict) or not keys.get("p256dh") or not keys.get("auth"):
        return None
    return {
        "endpoint": endpoint,
        "keys": {"p256dh": str(keys["p256dh"]), "auth": str(keys["auth"])},
    }


class PushService:
    def __init__(
        self,
        directory: Path,
        *,
        subject: str = DEFAULT_SUBJECT,
        sender: Sender | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.dir = directory
        self.subject = subject
        self._log = log
        self._sender = sender
        self._lock = threading.Lock()
        self._subs: list[dict[str, Any]] = []
        self._public_key: str | None = None
        self._load()

    # ---- subscriptions ----------------------------------------------------------------

    def _load(self) -> None:
        path = self.dir / "subscriptions.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._subs = [s for s in (_valid(x) for x in raw) if s is not None]

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "subscriptions.json.tmp"
        tmp.write_text(json.dumps(self._subs), encoding="utf-8")
        tmp.replace(self.dir / "subscriptions.json")

    def __len__(self) -> int:
        return len(self._subs)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        with self._lock:
            return iter(list(self._subs))

    def subscribe(self, sub: Any) -> int:
        clean = _valid(sub)
        if clean is None:
            raise ValueError("bad subscription")
        with self._lock:
            self._subs = [s for s in self._subs if s["endpoint"] != clean["endpoint"]]
            self._subs.append(clean)
            del self._subs[:-MAX_SUBSCRIPTIONS]
            self._save()
            return len(self._subs)

    def unsubscribe(self, endpoint: str) -> int:
        with self._lock:
            self._subs = [s for s in self._subs if s["endpoint"] != endpoint]
            self._save()
            return len(self._subs)

    # ---- VAPID ------------------------------------------------------------------------

    @property
    def available(self) -> bool:
        try:
            import pywebpush  # noqa: F401
        except ImportError:
            return False
        return True

    @property
    def public_key(self) -> str:
        """Base64url of the uncompressed P-256 point, what ``pushManager.subscribe`` wants."""
        if self._public_key is None:
            from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
            from py_vapid import Vapid02, b64urlencode

            self.dir.mkdir(parents=True, exist_ok=True)
            vapid = Vapid02.from_file(str(self.dir / "vapid.pem"))  # generates when absent
            raw = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
            self._public_key = str(b64urlencode(raw))
        return self._public_key

    def _default_sender(self, sub: dict[str, Any], payload: str) -> None:
        from pywebpush import WebPushException, webpush

        try:
            webpush(
                sub,
                payload,
                vapid_private_key=str(self.dir / "vapid.pem"),
                vapid_claims={"sub": self.subject},
                ttl=TTL_S,
                timeout=10,
            )
        except WebPushException as exc:
            resp = getattr(exc, "response", None)
            if resp is not None and getattr(resp, "status_code", 0) in (404, 410):
                raise SubscriptionGone(sub["endpoint"]) from exc
            raise

    # ---- sending ----------------------------------------------------------------------

    def notify(self, note: Notification) -> int:
        """Send to every device now; returns how many accepted it. Never raises."""
        sender = self._sender or self._default_sender
        payload = note.to_json()
        sent, gone = 0, []
        for sub in self:
            try:
                sender(sub, payload)
                sent += 1
            except SubscriptionGone:
                gone.append(sub["endpoint"])
            except Exception as exc:  # a push service hiccup must not take the watcher down
                self._log(f"push failed for {sub['endpoint'][:40]}…: {exc!r}")
        for ep in gone:
            self.unsubscribe(ep)
        return sent

    def notify_later(self, note: Notification) -> None:
        """Same, off the request thread (so an API call never waits on Apple/Google)."""
        threading.Thread(target=self.notify, args=(note,), daemon=True).start()
