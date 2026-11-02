"""Redact credentials before persisting untrusted agent input or reporting errors."""

import re

SECRET_KEY = re.compile(r"(?:token|password|secret|authorization|api[_-]?key|credential)", re.I)
SECRETS = (
    re.compile(r"(?i)\bBearer\s+[^\s\"',}]+"),
    re.compile(r"(?i)\b(?:token|password|secret|api[_-]?key)\s*[:=]\s*[^\s\"',}]+"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+)"),
    re.compile(r"https?://[^\s/@]+:[^\s/@]+@[^\s]+"),
    re.compile(r"https?://[^\s]+[?&](?:token|key|secret|password)=[^\s]+", re.I),
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----"),
)


def redact(value, known_secrets: tuple[str, ...] = ()):
    if isinstance(value, dict):
        return {
            redact(str(key), known_secrets): "[REDACTED]"
            if SECRET_KEY.search(str(key))
            else redact(item, known_secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, known_secrets) for item in value]
    if isinstance(value, str):
        for secret in known_secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        for pattern in SECRETS:
            value = pattern.sub("[REDACTED]", value)
    return value
