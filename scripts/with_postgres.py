"""Optional local test helper: uv run --with pgserver==0.1.4 python scripts/with_postgres.py pytest.

Starts a disposable PostgreSQL instance; no Docker daemon or system-wide service is required.
pgserver is deliberately not a production dependency. Existing databases are never touched.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

if len(sys.argv) < 2:
    raise SystemExit("usage: with_postgres.py COMMAND [ARG ...]")

temp_root = "/private/tmp" if sys.platform == "darwin" else None
with tempfile.TemporaryDirectory(prefix="factory-pg-", dir=temp_root) as directory:
    # Use the pinned package's bundled binaries directly. Its macOS runtime lock defaults
    # to Library/Caches, outside many development sandboxes.
    if configured_binaries := os.environ.get("FACTORY_POSTGRES_BIN"):
        binaries = Path(configured_binaries)
    else:
        import pgserver

        binaries = Path(pgserver.__file__).parent / "pginstall" / "bin"
    data = Path(directory) / "data"
    subprocess.run(
        [str(binaries / "initdb"), "-D", str(data), "-U", "postgres", "-A", "trust"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    print(
        subprocess.check_output([str(binaries / "postgres"), "--version"], text=True).strip(),
        flush=True,
    )
    (data / "postgresql.conf").write_text(
        f"listen_addresses = ''\nunix_socket_directories = '{directory}'\n"
    )
    subprocess.run(
        [str(binaries / "pg_ctl"), "-D", str(data), "-l", str(data / "log"), "-w", "start"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    try:
        url = f"postgresql+psycopg://postgres@/postgres?host={directory}"
        env = {**os.environ, "FACTORY_DATABASE_URL": url, "TEST_DATABASE_URL": url}
        result = subprocess.run(sys.argv[1:], env=env, check=False)
    finally:
        subprocess.run(
            [str(binaries / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
raise SystemExit(result.returncode)
