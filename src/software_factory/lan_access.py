"""Opt-in shared access behind a private-network reverse proxy."""

import hmac
import json
import os
import stat
from dataclasses import dataclass
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.parse import urlsplit

from .service import WEB_SESSION_PREFIX, DomainError
from .web_accounts import browser_write

PRIVATE_NETWORKS = tuple(
    ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


@dataclass(frozen=True, repr=False)
class LANAccess:
    origin: str
    networks: tuple
    proxy_secret: str
    user_token: str

    @classmethod
    def configured(cls):
        filename = os.getenv("FACTORY_LAN_ACCESS_FILE")
        if not filename:
            return None
        try:
            path = Path(filename)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise ValueError
            data = json.loads(path.read_text())
            if set(data) != {"origin", "networks", "proxy_secret", "user_token"}:
                raise ValueError
            origin = urlsplit(data["origin"])
            if (
                origin.scheme != "https"
                or not origin.hostname
                or origin.username
                or origin.password
                or origin.path
                or origin.query
                or origin.fragment
            ):
                raise ValueError
            address = ip_address(origin.hostname)
            if not isinstance(data["networks"], list) or not data["networks"]:
                raise ValueError
            networks = tuple(ip_network(value) for value in data["networks"])
            if not all(
                any(
                    net.version == private.version and net.subnet_of(private)
                    for private in PRIVATE_NETWORKS
                )
                for net in networks
            ) or not any(address in net for net in networks if address.version == net.version):
                raise ValueError
            for key in ("proxy_secret", "user_token"):
                if not isinstance(data[key], str) or not 32 <= len(data[key]) <= 256:
                    raise ValueError
            if data["user_token"].startswith(WEB_SESSION_PREFIX):
                raise ValueError
            return cls(data["origin"], networks, data["proxy_secret"], data["user_token"])
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise ValueError("Invalid owner-only FACTORY_LAN_ACCESS_FILE configuration") from None

    def token_for(self, request):
        if (
            not request.client
            or f"{request.url.scheme}://{request.url.netloc}" != self.origin
            or not hmac.compare_digest(
                request.headers.get("X-Factory-LAN-Key", "").encode(), self.proxy_secret.encode()
            )
        ):
            return None
        try:
            address = ip_address(request.client.host)
        except ValueError:
            return None
        if not any(address.version == net.version and address in net for net in self.networks):
            return None
        origin = request.headers.get("Origin")
        if (origin is not None and origin != self.origin) or request.headers.get(
            "Sec-Fetch-Site"
        ) == "cross-site":
            raise DomainError(403, "ORIGIN_DENIED")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            browser_write(request)
        return self.user_token
