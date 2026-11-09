"""Persistent shared operator access through an explicitly trusted Tailscale Serve origin."""

from .temporary_web_access import TemporaryWebAccess


class TailscaleAccess(TemporaryWebAccess):
    environment = "FACTORY_TAILSCALE_ACCESS_FILE"
    time_limited = False
