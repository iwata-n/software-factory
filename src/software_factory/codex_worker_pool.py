"""Resize independent Codex processes without interrupting accepted work."""

import time
from pathlib import Path
from uuid import uuid4

DEFAULT_CODEX_WORKERS = 10
MAX_CODEX_WORKERS = 32


class CodexWorkerPool:
    def __init__(self, directory, desired, provision, retire):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.desired = desired
        self.provision = provision
        self.retire = retire
        self.workers = {}
        self.retry_at = {}
        self.failures = {}

    def tick(self):
        target = self.desired()
        if type(target) is not int or not 1 <= target <= MAX_CODEX_WORKERS:
            raise ValueError("Invalid Codex worker count")
        now = time.monotonic()
        for slot, (process, drain) in list(self.workers.items()):
            if slot >= target:
                drain.touch(mode=0o600, exist_ok=True)
            else:
                drain.unlink(missing_ok=True)
            if process.poll() is None:
                continue
            self.retire(process)
            drain.unlink(missing_ok=True)
            del self.workers[slot]
            if slot < target:
                self.failures[slot] = self.failures.get(slot, 0) + 1
                self.retry_at[slot] = now + min(30, 2 ** min(self.failures[slot], 5))
        for slot in range(target):
            if slot in self.workers or now < self.retry_at.get(slot, 0):
                continue
            drain = self.directory / f"drain-{slot}-{uuid4().hex}"
            try:
                process = self.provision(slot, drain)
            except Exception:
                self.failures[slot] = self.failures.get(slot, 0) + 1
                self.retry_at[slot] = now + min(30, 2 ** min(self.failures[slot], 5))
                # Do not expose credentials or child-process diagnostics.
                print(f"Codex worker slot {slot + 1} could not start; retry scheduled", flush=True)
            else:
                self.workers[slot] = (process, drain)
