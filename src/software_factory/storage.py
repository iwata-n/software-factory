"""Tenant storage with directory-descriptor traversal and immutable uploaded bytes."""

import hashlib
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4


class StorageError(Exception):
    pass


def identifier(value: str) -> str:
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise StorageError("invalid storage identifier") from None
    return value


class LocalArtifactStorage:
    def __init__(self, root: Path | str):
        self.root = Path(root).absolute()
        for parent in (self.root, *self.root.parents):
            if parent.is_symlink():
                raise StorageError("symlink storage root")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    @contextmanager
    def directory(self, workspace_id):
        name = identifier(workspace_id)
        try:
            root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise StorageError("unsafe storage root") from None
        try:
            try:
                os.mkdir(name, mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            try:
                workspace_fd = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd
                )
            except OSError:
                raise StorageError("unsafe workspace storage") from None
            try:
                yield workspace_fd
            finally:
                os.close(workspace_fd)
        finally:
            os.close(root_fd)

    def put(self, workspace_id: str, artifact_id: str, body: bytes) -> None:
        name = identifier(artifact_id)
        with self.directory(workspace_id) as directory:
            temporary = name + "." + uuid4().hex + ".part"
            try:
                fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    mode=0o400,
                    dir_fd=directory,
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(
                        temporary,
                        name,
                        src_dir_fd=directory,
                        dst_dir_fd=directory,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    if self._read(directory, name) != body:
                        raise StorageError("immutable artifact conflict") from None
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _read(directory, name):
        import stat

        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                meta = os.fstat(stream.fileno())
                if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
                    raise StorageError("unsafe artifact file")
                return stream.read(16 * 1024 * 1024 + 1)
        except OSError:
            raise StorageError("artifact bytes unavailable") from None

    def read(self, workspace_id: str, artifact_id: str) -> bytes:
        with self.directory(workspace_id) as directory:
            return self._read(directory, identifier(artifact_id))

    def delete(self, workspace_id: str, artifact_id: str) -> None:
        with self.directory(workspace_id) as directory:
            name = identifier(artifact_id)
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
            for pending in os.listdir(directory):
                if pending.startswith(name + ".") and pending.endswith(".part"):
                    os.unlink(pending, dir_fd=directory)


def content_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
