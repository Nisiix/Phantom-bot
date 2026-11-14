"""One desk per data folder (2026-11-14).

On 2026-11-13 at 23:48 UTC two desks started 11 seconds apart on the same database (dashboards on 8080 and 8081).
The owner sold Mustache from one of them; the other still held it in memory and wrote it back, so the sold position
came back at the next start, and the first desk, stuck, kept port 8080 so its page never answered. A desk now takes
an exclusive lock on data/desk.lock for as long as it runs; a second start finds it taken and stops at once,
saying which process holds it. The operating system frees the lock when the process ends, even after a crash, so
a restart (exit code 75 -> start.bat starts it again) never finds a stale lock."""
from __future__ import annotations

import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, data_dir: Path, mode: str) -> None:
        self.path = Path(data_dir) / f"desk_{mode}.lock"
        self.fh = None

    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                fh.seek(1)               # byte 0 is the locked one (Windows): read what follows it
                held = fh.read().strip()
            except OSError:
                held = ""
            fh.close()
            raise AlreadyRunning(
                f"Il bot è già in esecuzione su questa cartella dati ({held or 'processo sconosciuto'}). "
                f"Un secondo avvio è stato fermato per non avere due bot sullo stesso conto. "
                f"Usa la finestra già aperta, oppure `python run.py restart` per riavviarlo.") from None
        # Lock taken: write who holds it, after the first byte (the byte Windows locks stays readable this way).
        fh.seek(0)
        fh.truncate()
        fh.write(f" PID {os.getpid()}")
        fh.flush()
        self.fh = fh
        return self

    def release(self) -> None:
        if self.fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.fh.close()
        self.fh = None
