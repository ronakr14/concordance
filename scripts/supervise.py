"""The launcher behind `make up` - what `docker compose up` used to do.

Three processes make a running system: the API, the job worker, and the Vite
dev server. Before containerization was cut, one compose file started all three
and one Ctrl-C stopped them. This script buys the same property natively:

    python tasks.py up              # foreground, Ctrl-C stops all three
    python tasks.py up DETACH=1     # background, `python tasks.py down` stops it

What it does, in order:

1. **Preflight** (`concordance preflight`). Nothing is spawned until the
   environment, the database, the migration state, the ports and `node_modules`
   are all known good. See `concordance/ops/preflight.py` for why.
2. **Migrate** (`concordance db upgrade head`). This is the resolution of the
   old "migrations on container startup, or a one-shot service?" question: one
   process runs them, before any process that needs them exists.
3. **Spawn** api, worker and web, and pump all three outputs into this
   terminal, each line labelled with its source and coloured.
4. **Stop them together.** Ctrl-C, or `make down` against a detached start,
   takes down the whole tree and leaves nothing behind.

Two Windows details drive the shape of the code. A child started from a `.cmd`
shim (npm) is a `cmd.exe` that spawns `node`, so terminating the child leaves
the grandchild running and holding port 5173 - hence `taskkill /T`, which ends
the tree. And Ctrl-C in a Windows console reaches every process in the console
group, so the children receive it at the same moment this script does; the
shutdown path is therefore written to be safe when the child is already gone.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
RUN_DIR = ROOT / ".run"
PIDFILE = RUN_DIR / "up.json"
LOGFILE = RUN_DIR / "up.log"
WINDOWS = os.name == "nt"

#: How long a child gets to exit on its own before the tree is killed.
GRACE_SECONDS = 10.0

# ANSI colours, one per process, so a glance separates three interleaved
# streams. Disabled when the output is redirected, where they are noise in a
# log file rather than colour in a terminal.
COLOURS = {"api": "\033[36m", "worker": "\033[35m", "web": "\033[32m", "up": "\033[33m"}
RESET = "\033[0m"


@dataclass
class Child:
    name: str
    argv: list[str]
    cwd: Path
    proc: subprocess.Popen[str] | None = None


def _readable_stdout() -> None:
    """Make this terminal able to print whatever the three children emit.

    Vite draws an arrow (U+279C) in its banner. On a Windows console still
    defaulting to cp1252 that arrow is unencodable, and the supervisor dies
    with a `UnicodeEncodeError` while relaying a child's perfectly healthy
    startup message. Relaying output must never be able to kill the relay, so
    anything the console cannot draw is replaced rather than raised.
    """
    with contextlib.suppress(AttributeError, OSError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _colour(name: str, text: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{COLOURS.get(name, '')}{text}{RESET}"


def _label(name: str, line: str, enabled: bool) -> str:
    return f"{_colour(name, f'{name:<7}', enabled)}| {line}"


def _pump(child: Child, sink: Queue[tuple[str, str | None]]) -> None:
    """Forward one child's output to the printer, then announce its exit.

    A `None` line is the end-of-stream marker. The printer needs it to know a
    process died: a supervisor that keeps two of three processes running and
    says nothing about the third is worse than no supervisor.
    """
    assert child.proc is not None and child.proc.stdout is not None
    for line in child.proc.stdout:
        sink.put((child.name, line.rstrip("\n")))
    sink.put((child.name, None))


# --------------------------------------------------------------------------
# stopping things
# --------------------------------------------------------------------------


def _kill_tree(pid: int) -> None:
    """End a process and everything it started.

    `Popen.terminate` ends only the direct child. On Windows that child is
    often a `cmd.exe` shim whose `node` grandchild survives and keeps the port;
    on POSIX the shell is `exec`-ed over, but the process group is still the
    safer target.
    """
    if WINDOWS:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(pid), signal.SIGTERM)


def _stop(children: list[Child], say) -> None:
    """Ask every child to stop, then insist. Idempotent: a dead child is fine."""
    alive = [c for c in children if c.proc is not None and c.proc.poll() is None]
    for child in alive:
        assert child.proc is not None
        say(f"stopping {child.name} (pid {child.proc.pid})")
        if WINDOWS:
            _kill_tree(child.proc.pid)
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(child.proc.pid), signal.SIGINT)
    deadline = time.monotonic() + GRACE_SECONDS
    for child in alive:
        assert child.proc is not None
        remaining = max(0.0, deadline - time.monotonic())
        try:
            child.proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            say(f"{child.name} did not stop in {GRACE_SECONDS:.0f}s - killing the tree")
            _kill_tree(child.proc.pid)
            try:
                child.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                say(f"{child.name} is still running as pid {child.proc.pid} - stop it by hand")


# --------------------------------------------------------------------------
# starting things
# --------------------------------------------------------------------------


def _spawn(child: Child) -> None:
    kwargs: dict[str, object] = {}
    if WINDOWS:
        # Its own group, so a console Ctrl-C does not race this script's own
        # orderly shutdown by killing the child first and leaving its tree.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    env = dict(os.environ, PYTHONUNBUFFERED="1", FORCE_COLOR="0")
    child.proc = subprocess.Popen(
        child.argv,
        cwd=str(child.cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        **kwargs,  # type: ignore[arg-type]
    )


def _children(py: str, npm: str, port: int, web_port: int) -> list[Child]:
    """api and worker are the same entrypoint module with different arguments.

    That property is what the shared container image used to demonstrate, and
    it is worth keeping: one package, one set of settings, one logging setup,
    and no chance of the two drifting into different views of the system.
    """
    return [
        Child("api", [py, "-m", "concordance.cli", "api", "serve", "--port", str(port)], ROOT),
        Child("worker", [py, "-m", "concordance.cli", "jobs", "worker"], ROOT),
        Child("web", [npm, "run", "dev", "--", "--port", str(web_port)], FRONTEND),
    ]


def _install_stop_signals() -> None:
    """Route every "please stop" signal through `KeyboardInterrupt`.

    Ctrl-C already arrives that way. The others do not: a detached start has no
    console to press Ctrl-C in, so it is stopped by a signal instead, and the
    default action for those is to die on the spot - leaving three orphaned
    children and a stale pidfile. Raising the same exception means one shutdown
    path serves all of them.
    """

    def stop(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    names = ["SIGINT", "SIGBREAK"] if WINDOWS else ["SIGINT", "SIGTERM", "SIGHUP"]
    for name in names:
        sig = getattr(signal, name, None)
        if sig is not None:
            # Not the main thread, or a signal this platform will not let us
            # take: neither is worth failing a start over.
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, stop)


def up(py: str, npm: str, port: int = 8000, web_port: int = 5173, skip_preflight: bool = False) -> int:
    colour = sys.stdout.isatty()
    _readable_stdout()
    _install_stop_signals()

    def say(message: str) -> None:
        print(_label("up", message, colour), flush=True)

    if not skip_preflight:
        say("preflight")
        rc = subprocess.call(
            [py, "-m", "concordance.cli", "preflight",
             "--ports", f"api:{port},web:{web_port}", "--frontend", str(FRONTEND)],
            cwd=str(ROOT),
        )
        if rc != 0:
            say("preflight failed - nothing was started")
            return rc
        say("migrations")
        rc = subprocess.call([py, "-m", "concordance.cli", "db", "upgrade", "head"], cwd=str(ROOT))
        if rc != 0:
            say("migration failed - nothing was started")
            return rc

    children = _children(py, npm, port, web_port)
    sink: Queue[tuple[str, str | None]] = Queue()
    for child in children:
        _spawn(child)
        assert child.proc is not None
        say(f"started {child.name} (pid {child.proc.pid})")
        threading.Thread(target=_pump, args=(child, sink), daemon=True).start()

    RUN_DIR.mkdir(exist_ok=True)
    PIDFILE.write_text(
        json.dumps(
            {
                "supervisor": os.getpid(),
                "children": {
                    c.name: c.proc.pid for c in children if c.proc is not None
                },
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "api": f"http://127.0.0.1:{port}",
                "web": f"http://127.0.0.1:{web_port}",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    say(f"api http://127.0.0.1:{port}/docs   web http://127.0.0.1:{web_port}   Ctrl-C stops all three")

    rc = 0
    open_streams = {c.name for c in children}
    try:
        while open_streams:
            try:
                name, line = sink.get(timeout=0.5)
            except Empty:
                # A child that died without closing its pipe (killed, rather
                # than exited) is noticed here rather than never.
                for child in children:
                    died = child.proc is not None and child.proc.poll() is not None
                    if died and child.name in open_streams and not _stream_open(child):
                        open_streams.discard(child.name)
                continue
            if line is None:
                child = next(c for c in children if c.name == name)
                # `wait`, not `poll`: the pipe closes a moment before the
                # process is reaped, and a poll that early reports `None`,
                # which reads as "still running" in the very line announcing
                # that it stopped.
                code = child.proc.wait(timeout=5) if child.proc else None
                say(f"{name} exited with code {code}")
                open_streams.discard(name)
                # One process leaving means the system is no longer running.
                # Take the rest down rather than leave a half-system up.
                rc = code or 1
                break
            print(_label(name, line, colour), flush=True)
    except KeyboardInterrupt:
        print("", flush=True)
        say("Ctrl-C - stopping")
    finally:
        _stop(children, say)
        PIDFILE.unlink(missing_ok=True)
        say("all stopped")
    return rc


def _stream_open(child: Child) -> bool:
    return child.proc is not None and child.proc.stdout is not None and not child.proc.stdout.closed


# --------------------------------------------------------------------------
# detached start, and stopping one
# --------------------------------------------------------------------------


def up_detached(py: str, port: int = 8000, web_port: int = 5173) -> int:
    """Start the same supervisor in the background, logging to `.run/up.log`.

    The preflight runs here, in the foreground, rather than inside the detached
    process: a start that fails should say so on the terminal that asked for
    it, not in a file the user does not yet know to read.
    """
    if PIDFILE.is_file():
        print(f"a start is already recorded in {PIDFILE} - run `python tasks.py down` first")
        return 1
    rc = subprocess.call(
        [py, "-m", "concordance.cli", "preflight",
         "--ports", f"api:{port},web:{web_port}", "--frontend", str(FRONTEND)],
        cwd=str(ROOT),
    )
    if rc != 0:
        print("preflight failed - nothing was started")
        return rc
    rc = subprocess.call([py, "-m", "concordance.cli", "db", "upgrade", "head"], cwd=str(ROOT))
    if rc != 0:
        print("migration failed - nothing was started")
        return rc

    RUN_DIR.mkdir(exist_ok=True)
    log = LOGFILE.open("w", encoding="utf-8")
    kwargs: dict[str, object] = {}
    if WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [py, str(Path(__file__)), "serve", "--port", str(port), "--web-port", str(web_port),
         "--skip-preflight"],
        cwd=str(ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        **kwargs,  # type: ignore[arg-type]
    )
    print(f"started detached as pid {proc.pid}; logs in {LOGFILE}")
    print("stop it with `python tasks.py down`")
    return 0


def down() -> int:
    """Stop a detached start, by the pids it recorded."""
    if not PIDFILE.is_file():
        print(f"nothing to stop - no {PIDFILE}")
        return 0
    try:
        state = json.loads(PIDFILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"{PIDFILE} is unreadable ({exc}); delete it and stop the processes by hand")
        return 1
    pids = [state.get("supervisor"), *state.get("children", {}).values()]
    for pid in [p for p in pids if isinstance(p, int)]:
        _kill_tree(pid)
    PIDFILE.unlink(missing_ok=True)
    print(f"stopped {len([p for p in pids if p])} process tree(s)")
    return 0


def status() -> int:
    if not PIDFILE.is_file():
        print("not running (no .run/up.json)")
        return 1
    print(PIDFILE.read_text(encoding="utf-8"))
    return 0


def _main(argv: list[str]) -> int:
    """`serve` is what a detached start re-enters; every command is called by tasks.py."""
    import argparse

    parser = argparse.ArgumentParser(prog="supervise")
    parser.add_argument("command", choices=["serve", "detached", "down", "status"])
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--web-port", type=int, default=5173)
    parser.add_argument("--skip-preflight", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "down":
        return down()
    if args.command == "status":
        return status()
    npm = "npm.cmd" if WINDOWS else "npm"
    if args.command == "detached":
        return up_detached(sys.executable, args.port, args.web_port)
    return up(sys.executable, npm, args.port, args.web_port, skip_preflight=args.skip_preflight)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
