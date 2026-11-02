"""Build a root-owned read-only candidate volume without extracting onto the host."""

import io
import posixpath
import tarfile
from pathlib import PurePosixPath

from .sandbox import RunnerError


def readonly_tar(archive):
    output = io.BytesIO()
    entries, links, total = [], {}, 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
            for info in source:
                path = info.name
                if (
                    not path
                    or PurePosixPath(path).is_absolute()
                    or "\\" in path
                    or "\x00" in path
                    or any(p in {"", ".", ".."} or p.lower() == ".git" for p in path.split("/"))
                ):
                    raise RunnerError("CANDIDATE_PATH_DENIED", "POLICY")
                if info.isdir():
                    continue
                if info.isfile():
                    total += info.size
                    if total > 128 * 1024 * 1024:
                        raise RunnerError("CANDIDATE_TOO_LARGE", "POLICY")
                    body = source.extractfile(info).read()
                elif info.issym():
                    links[path] = info.linkname
                    body = None
                else:
                    raise RunnerError("CANDIDATE_LINK_DENIED", "POLICY")
                entries.append((info, body))
        names = [i.name for i, _ in entries]
        if len(set(names)) != len(names):
            raise RunnerError("CANDIDATE_DUPLICATE_PATH", "POLICY")
        for name in names:
            if any(name.startswith(link + "/") for link in links):
                raise RunnerError("CANDIDATE_LINK_DENIED", "POLICY")
        for name in links:
            seen, current = {name}, name
            while current in links:
                link = links[current]
                if link.startswith("/") or "\\" in link or "\x00" in link:
                    raise RunnerError("CANDIDATE_LINK_DENIED", "POLICY")
                current = posixpath.normpath(posixpath.join(posixpath.dirname(current), link))
                if current in seen or current == ".." or current.startswith("../"):
                    raise RunnerError("CANDIDATE_LINK_DENIED", "POLICY")
                seen.add(current)
        with tarfile.open(fileobj=output, mode="w") as target:
            for original, body in entries:
                info = tarfile.TarInfo(original.name)
                info.mode = 0o555 if original.mode & 0o111 else 0o444
                if body is None:
                    info.type, info.linkname = tarfile.SYMTYPE, original.linkname
                else:
                    info.size = len(body)
                target.addfile(info, io.BytesIO(body) if body is not None else None)
        return output.getvalue()
    except (tarfile.TarError, ValueError, OSError):
        raise RunnerError("CANDIDATE_ARCHIVE_INVALID", "POLICY") from None
