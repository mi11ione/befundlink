"""Background execution, so no outside-person request ever waits for HAPI, the bot or the validator.

Outbox      - ONE thread, strictly in submission order: HAPI AuditEvent POSTs, Task PUTs, bot /notify.
              Order matters: a Task PUT is submitted before any AuditEvent referencing it. Retries with
              backoff; an AuditEvent that still fails stays recorded and is re-sent by sync_audit.
AnswerWorker - ONE thread for the filing pipeline (re-check -> build -> validate -> prove -> transaction ->
              notify). Drains whatever is queued into one batch, so a CLI fallback validates several
              filings in a single JVM. One worker keeps filings (and their audit entries) in order.
inline=True (tests) runs everything synchronously in the caller, keeping tests deterministic.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable

log = logging.getLogger("pgk.workers")


class Outbox:
    def __init__(self, inline: bool, retries: tuple[float, ...] = (0.5, 1.0, 2.0)):
        self.inline, self.retries = inline, retries
        self.q: queue.Queue = queue.Queue()
        self._t: threading.Thread | None = None

    def start(self) -> None:
        if not self.inline and not self._t:
            self._t = threading.Thread(target=self._loop, name="outbox", daemon=True)
            self._t.start()

    def put(self, fn: Callable, *args, desc: str = "", **kw) -> None:
        if self.inline:
            self._run(fn, args, kw, desc)
        else:
            self.q.put((fn, args, kw, desc))

    def _run(self, fn, args, kw, desc) -> None:
        for i, wait in enumerate((0.0, *self.retries)):
            if wait and not self.inline:
                time.sleep(wait)
            try:
                if fn(*args, **kw) is not False:
                    return
            except Exception as e:
                log.warning("outbox %s attempt %d failed: %s", desc or fn.__name__, i + 1, e)
            if self.inline:
                return
        log.error("outbox %s gave up after %d attempts", desc or getattr(fn, "__name__", "?"), len(self.retries) + 1)

    def _loop(self) -> None:
        while True:
            fn, args, kw, desc = self.q.get()
            try:
                self._run(fn, args, kw, desc)
            finally:
                self.q.task_done()

    def drain(self, timeout: float = 30) -> bool:
        """Wait until everything submitted so far is done (tests, shutdown)."""
        t0 = time.monotonic()
        while self.q.unfinished_tasks and time.monotonic() - t0 < timeout:
            time.sleep(0.05)
        return not self.q.unfinished_tasks


class AnswerWorker:
    def __init__(self, process_batch: Callable[[list[tuple]], None], inline: bool, max_batch: int = 16):
        self.process_batch, self.inline, self.max_batch = process_batch, inline, max_batch
        self.q: queue.Queue = queue.Queue()
        self._t: threading.Thread | None = None

    def start(self) -> None:
        if not self.inline and not self._t:
            self._t = threading.Thread(target=self._loop, name="answer-worker", daemon=True)
            self._t.start()

    def submit(self, rid: str, aid: int, override: dict | None = None) -> None:
        if self.inline:
            self.process_batch([(rid, aid, override)])
        else:
            self.q.put((rid, aid, override))

    def _loop(self) -> None:
        while True:
            batch = [self.q.get()]
            time.sleep(0.05)  # let simultaneous uploads join the same batch
            while len(batch) < self.max_batch:
                try:
                    batch.append(self.q.get_nowait())
                except queue.Empty:
                    break
            try:
                self.process_batch(batch)
            except Exception:  # process_batch handles per-item errors; this is a last resort
                log.exception("answer batch failed")
            finally:
                for _ in batch:
                    self.q.task_done()

    def drain(self, timeout: float = 60) -> bool:
        t0 = time.monotonic()
        while self.q.unfinished_tasks and time.monotonic() - t0 < timeout:
            time.sleep(0.05)
        return not self.q.unfinished_tasks
