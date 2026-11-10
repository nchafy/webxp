#!/usr/bin/env python3
"""One loopback HTTP server for the harnesses in `scripts/`, on a port nobody chose.

Dev-time only. Nothing under `src/`, `apps/` or `shared/` may import this module: the app is
a static tree that any server can host, and `scripts/` is not part of that contract. A helper
this convenient acquires callers it should not have, so the rule is written here rather than
left to be inferred.

Seven harnesses each started their own `http.server` on a hard-coded port and polled until
something answered. Two defects came with that, and both are fixed once here instead of
seven times:

* A hard-coded port is a guess. Two runs out of this one checkout collide, and the later one
  silently measures the earlier one's server.
* A readiness poll is satisfied by anything answering 200. `serving_this_tree` is what
  separates "something answered" from "this tree answered", and it used to live in
  `check.py` alone — so the inspectors, the instruments reached for when a measurement is
  already in doubt, had none of it.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent

#: `http.server`'s own announcement of the port it bound, e.g.
#: `Serving HTTP on 127.0.0.1 port 52229 (http://127.0.0.1:52229/) ...`, measured on
#: CPython 3.8.19. The number is read out of this line rather than chosen by us: see `serve`.
PORT_LINE = re.compile(r"\bport (\d+)\b")

#: How long to wait for that line. The server prints it immediately after `bind()`, so this
#: only expires when the server is wedged before serving — which must be reported, not
#: waited on forever. A `readline` with no bound turns that into a hang, and a harness that
#: hangs has not failed, it has stopped saying anything.
PORT_LINE_TIMEOUT_S = 15.0

#: The exit status a killed server reports, so the timeout can be named as the cause rather
#: than left as a bare negative number in the error.
KILLED = -9

#: Files the harnesses are built on, fetched over HTTP and compared to disk before anything
#: is measured. The page itself, plus the two modules whose fields every geometry assertion
#: indexes into.
TREE_WITNESSES = ("index.html", "src/window.mjs", "src/desktop.mjs", "src/icons.mjs")


class ServeError(RuntimeError):
    """The server could not be started, or what answered on its port is not this tree."""


def serving_this_tree(base: str) -> list[str]:
    """Prove the server answering on this port is serving *this* working tree.

    A server is ready as soon as something answers 200 on the port, and `http.server` exits
    with `Address already in use` rather than failing loudly where the harness can see it. So
    a second process already on that port — a peer's temp checkout at another revision, a
    leftover server from an earlier run, another agent in the same tree — silently becomes
    the thing every check measures. `docs/review.md`: testing a stale copy and reporting
    clean is the worst outcome available.

    Checking the subprocess is still alive is not enough and was tried: the *other* server
    answers the first 200 while ours is still on its way to exiting, so `poll()` is `None`
    and the inference is wrong. Measured against a decoy on the port — it passed the process
    check and the run went on to report `window.webxp missing`, blaming the page for a server
    mix-up.

    So this measures instead of inferring: fetch the bytes and compare them to the file on
    disk. That also catches an HTTP cache serving a stale module, which is the same defect
    arriving by a different route.

    This is the diagnosis for one run that was never explained at the time: `placed` came
    back `{}`, `cascadeSlot` undefined, `Desktop.destroy` not a function and zero `var()`
    fills in the icon set — four independent symptoms of a page built from older modules —
    while every file on disk was newer than the failure and the next run was clean.
    """
    bad = []
    for name in TREE_WITNESSES:
        path = REPO_ROOT / name
        if not path.is_file():
            bad.append(f"{name} is not in {REPO_ROOT} — the harness is pointed at the wrong tree")
            continue
        try:
            with urllib.request.urlopen(f"{base}/{name}", timeout=10) as answer:
                served = answer.read()
        except OSError as error:
            bad.append(f"{base}/{name} could not be fetched — {error}")
            continue
        on_disk = path.read_bytes()
        if served != on_disk:
            bad.append(
                f"{base}/{name} served {len(served)} bytes "
                f"(sha256 {hashlib.sha256(served).hexdigest()[:12]}) but {name} on disk is "
                f"{len(on_disk)} bytes (sha256 {hashlib.sha256(on_disk).hexdigest()[:12]}). "
                "Something other than this tree is answering on that port, or a cache is "
                "between the two. Every check would measure that copy, not this one — "
                "drop --port and let the kernel pick a port, which cannot collide."
            )
    return bad


def _announced_port(server: subprocess.Popen, log) -> int:
    """The port out of the server's first line of stdout, or `ServeError`.

    The read is bounded by killing the server rather than by abandoning the read: a server
    that has exited is at EOF, so `readline` returns at once and the one thread that touches
    `stdout` is this one. A watchdog thread holding the pipe open instead would race the
    close in `serve`.
    """
    watchdog = threading.Timer(PORT_LINE_TIMEOUT_S, server.kill)
    watchdog.start()
    try:
        line = server.stdout.readline()
    finally:
        watchdog.cancel()

    found = PORT_LINE.search(line)
    if found:
        return int(found.group(1))

    status = server.poll()
    log.seek(0)
    why = (
        f"it was killed after {PORT_LINE_TIMEOUT_S}s without printing one" if status == KILLED
        else f"it exited with {status} and {log.read().strip() or 'wrote nothing to stderr'}"
    )
    raise ServeError(
        "http.server did not report a port, so there is no port to measure against: "
        f"{why}. Its first line of stdout was {line!r}."
    )


@contextlib.contextmanager
def serve(port: int | None = None) -> Iterator[str]:
    """Serve `REPO_ROOT` over loopback and yield its base URL, stopping it on the way out.

    With no `port`, the kernel picks a free one. That is the default because it is the only
    choice that cannot collide: two runs of this checkout, or a leftover server from an
    earlier one, get different ports instead of quietly sharing one. A fixed `port` is what
    you pass when you want to attach to the page yourself — it is `--port` on every caller.

    The number is read back off the server's own announcement, never chosen here. Binding a
    probe socket to find a free port, closing it and handing the number on leaves a window in
    which a second run of this same checkout takes it, and that is the one collision
    `serving_this_tree` cannot catch: both runs serve identical bytes, so the comparison
    passes and two browsers share one server.

    No readiness poll. `http.server` binds and listens before it prints, so the announcement
    *is* readiness, and the first fetch below is a measurement rather than a guess at one.

    Raises `ServeError` when the server reports no port, or when what answers is not this
    tree. Neither falls back to a guess: an unreachable server is a failure, never a skip.
    """
    # Loopback only. Nothing here is meant to be reachable from another machine.
    #
    # stderr is a temp file, not DEVNULL, so a bind failure under an explicit `--port` can be
    # quoted back instead of inferred. Request logs land there too; the file is only read when
    # no port arrived, which is before any request could have been served.
    log = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    server = subprocess.Popen(
        [sys.executable, "-u", "-m", "http.server", str(port or 0), "--bind", "127.0.0.1"],
        cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=log, text=True,
    )
    try:
        base = f"http://127.0.0.1:{_announced_port(server, log)}"
        wrong_tree = serving_this_tree(base)
        if wrong_tree:
            raise ServeError("\n".join(wrong_tree))
        yield base
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=10)
        server.stdout.close()
        log.close()
