"""Read-only metadata inspection for the trusted worker source boundary."""

import os
from pathlib import Path


def symlink_paths(root: Path) -> list[str]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("repository root must be a real directory")
    found = []
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(parent) / name
            if path.is_symlink():
                found.append(path.relative_to(root).as_posix())
    return sorted(found)
