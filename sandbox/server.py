"""Start the real sandbox (a uvicorn subprocess) on a throwaway DB and a free port.

Shared by the tests and the eval harness, so both exercise the app exactly as `make sandbox` serves it.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import httpx

from sandbox import seed

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def sandbox_server(workdir: Path, chaos: str = "", **extra_env: str) -> Iterator[str]:
    """Fresh seeded DB at `workdir/acme.db`; yields the server's base URL."""
    workdir.mkdir(parents=True, exist_ok=True)
    db_file = workdir / "acme.db"
    seed.reset(db_file)
    port = _free_port()
    env = {**os.environ, "ACME_DB": str(db_file), "CHAOS": chaos, **extra_env}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "sandbox.app:app", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(f"{base}/health", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            raise RuntimeError("sandbox did not start")
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)
