"""Time-limited shared operator access through the configured Tailscale Serve origin."""

import json
import math
import os
import stat
import time
from dataclasses import dataclass
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.parse import urlsplit

from .service import WEB_SESSION_PREFIX, DomainError
from .web_accounts import browser_write

TAILNET_NETWORKS = (ip_network("100.64.0.0/10"), ip_network("fd7a:115c:a1e0::/48"))


@dataclass(frozen=True, repr=False)
class TemporaryWebAccess:
    origin: str
    user_token: str
    expires_at: float

    @classmethod
    def configured(cls):
        filename = os.getenv("FACTORY_TEMPORARY_WEB_ACCESS_FILE")
        if not filename:
            return None
        try:
            path = Path(filename)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise ValueError
            data = json.loads(path.read_text())
            if set(data) != {"origin", "user_token", "expires_at"}:
                raise ValueError
            origin = urlsplit(data["origin"])
            if (
                origin.scheme != "https"
                or not origin.hostname
                or not origin.hostname.endswith(".ts.net")
                or origin.username
                or origin.password
                or origin.path
                or origin.query
                or origin.fragment
                or origin.port not in {None, 443}
            ):
                raise ValueError
            token = data["user_token"]
            if (
                not isinstance(token, str)
                or not 32 <= len(token) <= 256
                or token.startswith(WEB_SESSION_PREFIX)
            ):
                raise ValueError
            expires = float(data["expires_at"])
            if not math.isfinite(expires) or expires > time.time() + 86400:
                raise ValueError
            return cls(data["origin"], token, expires)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise ValueError(
                "Invalid owner-only FACTORY_TEMPORARY_WEB_ACCESS_FILE configuration"
            ) from None

    def token_for(self, request):
        if (
            time.time() >= self.expires_at
            or not request.client
            or f"{request.url.scheme}://{request.url.netloc}" != self.origin
            or request.headers.get("X-Forwarded-Proto") != "https"
            or request.headers.get("X-Forwarded-Host") != request.url.netloc
            or request.headers.get("Tailscale-Funnel-Request") is not None
        ):
            return None
        try:
            address = ip_address(request.client.host)
        except ValueError:
            return None
        if not any(address.version == net.version and address in net for net in TAILNET_NETWORKS):
            return None
        if (
            request.headers.get("Origin", self.origin) != self.origin
            or request.headers.get("Sec-Fetch-Site") == "cross-site"
        ):
            raise DomainError(403, "ORIGIN_DENIED")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            browser_write(request)

        def validate(session, actor):
            if actor.user_access != "OPERATOR":
                raise DomainError(403, "TEMPORARY_OPERATOR_REQUIRED")

        request.app.state.service.read(self.user_token, validate)
        return self.user_token
