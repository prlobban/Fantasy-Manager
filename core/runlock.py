"""The run lock, portable: fcntl.flock on POSIX, msvcrt.locking on Windows.

cron_manage.sh holds data/run.lock for the length of a task, the watcher asks
whether a sweep holds it, and repair.py takes it to deploy. On the box all
three use flock(2). Windows has neither fcntl nor a flock binary, so this
module is the one lock both platforms share.

    python -m core.runlock <lockfile> <wait_s> -- <cmd...>

holds the lock while <cmd> runs (cron_manage.sh's fallback where no `flock`
exists) and exits with <cmd>'s code. Like `flock -w`, a wait that times out
says so and runs anyway.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

if os.name == "nt":
    import msvcrt

    def _try(fh) -> bool:
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _release(fh) -> None:
        fh.seek(0)
        with contextlib.suppress(OSError):
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try(fh) -> bool:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def _release(fh) -> None:
        fcntl.flock(fh, fcntl.LOCK_UN)


@contextlib.contextmanager
def hold(path: Path, wait_s: float = 0, *, poll_s: float = 5, strict: bool = True):
    """Hold the lock. Waits up to wait_s; on timeout raises TimeoutError, or
    with strict=False yields False (unlocked) so the caller can run anyway."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as fh:
        deadline = time.monotonic() + wait_s
        got = _try(fh)
        while not got and time.monotonic() < deadline:
            time.sleep(poll_s)
            got = _try(fh)
        if not got and strict:
            raise TimeoutError(f"run lock {path} held too long")
        try:
            yield got
        finally:
            if got:
                _release(fh)


def is_held(path: Path) -> bool:
    """True if another process holds the lock right now."""
    path = Path(path)
    if not path.exists():
        return False
    with path.open("a+") as fh:
        if _try(fh):
            _release(fh)
            return False
        return True


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[2] != "--":
        print("usage: python -m core.runlock <lockfile> <wait_s> -- <cmd...>", file=sys.stderr)
        return 2
    lock, wait_s, cmd = Path(argv[0]), float(argv[1]), argv[3:]
    # Windows' CreateProcess will not resolve "./.venv/Scripts/python.exe".
    if exe := shutil.which(cmd[0]):
        cmd = [os.path.abspath(exe), *cmd[1:]]
    with hold(lock, wait_s, strict=False) as got:
        if not got:
            print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} "
                  "⚠️ run lock wait timed out — running anyway", flush=True)
        return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
