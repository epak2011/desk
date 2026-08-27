"""Fail-closed email delivery adapter for the notification worker."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass


class DeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeliveryConfig:
    enabled: bool
    api_key: str
    from_email: str

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.api_key and self.from_email)


def config_from_env() -> DeliveryConfig:
    enabled = os.environ.get("NOTIFICATIONS_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    return DeliveryConfig(
        enabled=enabled,
        api_key=os.environ.get("RESEND_API_KEY", "").strip(),
        from_email=os.environ.get("NOTIFICATION_FROM_EMAIL", "").strip(),
    )


def send_email(*, recipient: str, subject: str, html: str, config: DeliveryConfig | None = None) -> str:
    settings = config or config_from_env()
    if not settings.ready:
        raise DeliveryError("Email delivery is disabled or incompletely configured.")
    if "@" not in str(recipient or ""):
        raise DeliveryError("Recipient email is invalid.")
    request = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps({
            "from": settings.from_email,
            "to": [recipient],
            "subject": subject,
            "html": html,
        }).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise DeliveryError(f"Email provider rejected the request ({exc.code}).") from None
    except Exception:
        raise DeliveryError("Email provider is temporarily unavailable.") from None
    provider_id = str(payload.get("id") or "").strip()
    if not provider_id:
        raise DeliveryError("Email provider returned no delivery ID.")
    return provider_id
