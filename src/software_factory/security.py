"""Redact credentials before persisting untrusted agent input or reporting errors."""

import re
from collections import Counter
from pathlib import PurePosixPath

SECRET_KEY = re.compile(r"(?:token|password|secret|authorization|api[_-]?key|credential)", re.I)
SECRETS = (
    re.compile(r"(?i)\bBearer\s+[^\s\"',}]+"),
    re.compile(r"(?i)\b(?:token|password|secret|api[_-]?key)\s*[:=]\s*[^\s\"',}]+"),
    re.compile(r"\bxox[a-z]-[A-Za-z0-9-]+"),
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


def introduces_secret(path, old, new):
    code = PurePosixPath(path).suffix in {".py", ".js", ".ts", ".jsx", ".tsx"}
    for index, pattern in enumerate(SECRETS):
        if code and index == 1:
            continue  # Unquoted variable bindings and type annotations are code.

        def matches(text, pattern=pattern, index=index):
            return Counter(
                match[0]
                for match in pattern.finditer(text)
                if not (code and index == 0 and match[0].split(None, 1)[-1].startswith(("{", "${")))
            )

        if matches(new) - matches(old):
            return True
    return False


def sensitive_patch(patch, known_secrets=()):
    if any(secret and secret in patch for secret in known_secrets):
        return True
    path, pieces = "", []
    for line in patch.splitlines(keepends=True):
        if line.startswith("--- "):
            if introduces_secret(path, "", "".join(pieces)):
                return True
            path, pieces = line[4:].strip().removeprefix("a/"), []
            if redact(path) != path:
                return True
        elif line.startswith("+++ "):
            target = line[4:].strip().removeprefix("b/")
            if redact(target) != target:
                return True
            if target != "/dev/null":
                path = target
        elif line.startswith("+"):
            pieces.append(line[1:])
    return introduces_secret(path, "", "".join(pieces))
