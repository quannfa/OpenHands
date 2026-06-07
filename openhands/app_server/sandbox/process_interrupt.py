"""Interrupt agent-spawned commands inside a Docker sandbox.

This module backs the "Stop" button: when the user clicks Stop we want to
interrupt the commands the agent is currently running inside the sandbox
(``npm install``, ``sleep``, ``python ...``, …) without disturbing the
sandbox container itself — code-server, ssh, user-started services, and the
agent-server itself must keep running so the user can immediately send a
follow-up message.

Why only the tmux path
======================
The agent-server runs every shell command inside a tmux pane (using a
private socket ``-Lopenhands``). That gives us an *unambiguous* handle on
which processes belong to the agent's current task:

  pane_pid (the bash shell at the head of the pane)
    └── any child of pane_pid is a command the agent started

We previously also had a "process-table scan" fallback that signalled every
process whose ``comm`` was not on an allow-list. That fallback proved
fundamentally unsafe: in real sandboxes the agent-server worker, the
code-server child, the VS Code ``node`` process and the bash shells the
agent uses are all called either ``openhands-agent``, ``sh``, ``bash`` or
``node`` — depending on the angle they're indistinguishable from "an agent
command". A single misclassification kills PID 1's child and the whole
container exits. So the fallback is gone: if tmux is unavailable or has no
panes, we do nothing and return a result that says so. Best-effort, fail
safe.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass, field

import docker
from docker.errors import APIError, NotFound

_logger = logging.getLogger(__name__)


# --- timing knobs (kept as module-level constants for easy tuning) ----------

SIGINT_GRACE_SECONDS = 2.0
"""How long to wait for processes to exit after SIGINT (simulated Ctrl+C)."""

SIGTERM_GRACE_SECONDS = 3.0
"""How long to wait for processes to exit after SIGTERM (cleanup window)."""

SIGKILL_REAP_SECONDS = 1.0
"""Time given for SIGKILL'd processes to actually disappear from the table."""

POLL_INTERVAL_SECONDS = 0.1
"""How often we re-check whether a PID still exists during the grace period."""

EXEC_TIMEOUT_SECONDS = 10.0
"""Hard upper bound on any single ``docker exec`` invocation."""


# --- tmux configuration -----------------------------------------------------

# The agent-server's tmux is launched as ``tmux -Lopenhands ...``, so every
# subsequent tmux command MUST also include ``-Lopenhands`` to reach the
# right socket. We pin it as a constant so the call sites stay readable.
TMUX_SOCKET_FLAG = ('tmux', '-Lopenhands')

# Plus: the agent-server overrides ``TMUX_TMPDIR`` so the socket dir is NOT
# the default ``/tmp/tmux-<uid>`` but something like
# ``/tmp/openhands-agent-server-<worker_pid>/tmux-<uid>/openhands``. If we
# call ``tmux -Lopenhands ...`` without that env var we silently get
# "error connecting to /tmp/tmux-<uid>/openhands (No such file or directory)"
# and list-panes returns nothing — which would then look like "no work in
# flight" and Stop becomes a no-op. We discover ``TMUX_TMPDIR`` at runtime
# by reading the tmux server process's environment from ``/proc``.


# --- result type ------------------------------------------------------------


@dataclass
class InterruptResult:
    """Outcome of an interrupt run, surfaced to the caller for logging."""

    tmux_pids_seen: int = 0
    candidate_pids: int = 0
    sigint_killed: int = 0
    sigterm_killed: int = 0
    sigkill_killed: int = 0
    survivors: list[int] = field(default_factory=list)
    error: str | None = None

    @property
    def total_killed(self) -> int:
        return self.sigint_killed + self.sigterm_killed + self.sigkill_killed


# --- public entry point -----------------------------------------------------


async def interrupt_sandbox_processes(
    docker_client: docker.DockerClient,
    container_name: str,
) -> InterruptResult:
    """Interrupt the agent's working processes inside ``container_name``.

    The container itself stays running. Returns an :class:`InterruptResult`
    describing what we did so the caller can log it. Never raises on the
    normal "process already exited" / "no agent work running" paths.
    """
    result = InterruptResult()
    try:
        container = docker_client.containers.get(container_name)
    except NotFound:
        result.error = 'container_not_found'
        return result
    except APIError as exc:
        result.error = f'docker_api_error:{exc}'
        return result

    if container.status != 'running':
        # Nothing to do — and ``docker exec`` against a non-running container
        # raises. We treat this as a no-op success.
        result.error = f'container_not_running:{container.status}'
        return result

    # --- 1. Collect agent command PIDs via tmux ---------------------------
    candidate_pids = await _collect_via_tmux(container, result)
    result.candidate_pids = len(candidate_pids)
    if not candidate_pids:
        # Either tmux isn't running yet, or no agent command is in flight.
        # Both are legitimate "nothing to do" states. We deliberately do NOT
        # fall back to a full process-table scan — see the module docstring
        # for the safety argument.
        return result

    # --- 2. Signal escalation: SIGINT -> SIGTERM -> SIGKILL ---------------
    survivors = await _signal_and_wait(
        container, candidate_pids, 'INT', SIGINT_GRACE_SECONDS
    )
    result.sigint_killed = len(candidate_pids) - len(survivors)
    if not survivors:
        return result

    _logger.warning(
        'sandbox=%s: %d process(es) ignored SIGINT, escalating to SIGTERM: %s',
        container_name,
        len(survivors),
        survivors,
    )
    survivors = await _signal_and_wait(
        container, survivors, 'TERM', SIGTERM_GRACE_SECONDS
    )
    result.sigterm_killed = (
        result.candidate_pids - result.sigint_killed - len(survivors)
    )
    if not survivors:
        return result

    _logger.error(
        'sandbox=%s: %d process(es) ignored SIGTERM, sending SIGKILL: %s',
        container_name,
        len(survivors),
        survivors,
    )
    final_survivors = await _signal_and_wait(
        container, survivors, 'KILL', SIGKILL_REAP_SECONDS
    )
    result.sigkill_killed = len(survivors) - len(final_survivors)
    result.survivors = final_survivors
    if final_survivors:
        # Extremely rare — usually means a D-state (uninterruptible disk IO)
        # process. There is nothing usermode can do about it; log and move on.
        _logger.error(
            'sandbox=%s: %d process(es) survived SIGKILL (likely D-state): %s',
            container_name,
            len(final_survivors),
            final_survivors,
        )
    return result


# --- helpers: tmux env discovery --------------------------------------------


async def _discover_tmux_env(container) -> dict[str, str]:
    """Return env vars needed to reach the agent-server's tmux socket.

    Currently that's just ``TMUX_TMPDIR`` (and only if the tmux server set
    one — when it didn't, we return an empty dict and tmux uses its default
    ``/tmp/tmux-<uid>`` location).

    We locate the tmux server by ``pgrep`` and then read ``/proc/<pid>/environ``.
    If anything fails we return ``{}`` — the caller will then run tmux with
    the default tmpdir, which is the safe degraded behaviour for
    non-agent-server sandboxes (e.g. unit-test stubs).
    """
    # ``pgrep -f tmux`` matches every process whose full command line contains
    # the string "tmux" (server and clients alike). We deliberately don't try
    # to be more precise because ``pgrep -x`` requires a whole-string match
    # against either the 15-char ``comm`` field (where the server reads as
    # "tmux: server", not "tmux") OR the full command line (which is
    # ``/usr/bin/tmux -Lopenhands new-session ...``). Both whole-string forms
    # are surprisingly hard to get right across pgrep versions. The broad
    # substring match is fine: we then iterate and pick the first PID whose
    # ``/proc/<pid>/environ`` carries TMUX_TMPDIR.
    exit_code, output = await _exec(container, ['pgrep', '-f', 'tmux'])
    if exit_code != 0 or not output.strip():
        return {}

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid = int(line)
        except ValueError:
            continue
        env = await _read_proc_environ(container, pid)
        tmpdir = env.get('TMUX_TMPDIR')
        if tmpdir:
            return {'TMUX_TMPDIR': tmpdir}
    return {}


async def _read_proc_environ(container, pid: int) -> dict[str, str]:
    """Parse ``/proc/<pid>/environ`` (NUL-separated KEY=VAL pairs)."""
    exit_code, output = await _exec(container, ['cat', f'/proc/{pid}/environ'])
    if exit_code != 0:
        return {}
    env: dict[str, str] = {}
    # ``cat`` over docker exec returns the raw bytes decoded as utf-8 by _exec;
    # the NULs survive the round-trip as embedded '\x00'.
    for entry in output.split('\x00'):
        if '=' not in entry:
            continue
        key, _, value = entry.partition('=')
        if key:
            env[key] = value
    return env


# --- helpers: candidate collection ------------------------------------------


async def _collect_via_tmux(container, result: InterruptResult) -> list[int]:
    """Find PIDs of commands currently running inside any tmux pane.

    We deliberately return the *children* of each pane's shell, never the
    shell itself. Killing the shell would close the tmux pane and break the
    agent's terminal pool; killing only the children is the equivalent of
    Ctrl+C inside the pane.

    Returns an empty list if tmux is not running, the socket is unreachable,
    or every pane is idle — the caller must treat that as "nothing to do".
    """
    # 0) Discover the tmux server PID and its TMUX_TMPDIR. The agent-server
    #    overrides this env var, so calling ``tmux -Lopenhands`` without it
    #    would silently miss the socket. See the module-level note above
    #    ``_discover_tmux_env`` for the rationale.
    tmux_env = await _discover_tmux_env(container)

    # 1) list pane PIDs (the bash shell at the head of each pane)
    list_panes_cmd = list(TMUX_SOCKET_FLAG) + [
        'list-panes',
        '-a',
        '-F',
        '#{pane_pid}',
    ]
    exit_code, output = await _exec(container, list_panes_cmd, env=tmux_env)
    if exit_code != 0 or not output.strip():
        # tmux not installed, socket missing, or no panes. Either way: bail.
        return []

    pane_pids: list[int] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pane_pids.append(int(line))
        except ValueError:
            continue
    result.tmux_pids_seen = len(pane_pids)
    if not pane_pids:
        return []

    # 2) for each pane pid, list direct children. Anything reported is a
    #    command the agent currently has running in that pane.
    pane_pid_set = set(pane_pids)
    children: list[int] = []
    for pane_pid in pane_pids:
        exit_code, output = await _exec(
            container,
            ['ps', '-o', 'pid=', '--ppid', str(pane_pid)],
        )
        if exit_code != 0:
            continue
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pid = int(line)
            except ValueError:
                continue
            if pid in pane_pid_set:
                # Defensive: don't kill the shell itself
                continue
            children.append(pid)
    return children


# --- helpers: signal + wait -------------------------------------------------


async def _signal_and_wait(
    container,
    pids: list[int],
    signal_name: str,
    grace_seconds: float,
) -> list[int]:
    """Send ``signal_name`` to each PID (as a process group), then poll for exit.

    Returns the list of PIDs that were still alive when the grace period elapsed.
    Signals are sent via ``killpg`` so the entire descendant tree of the bash
    command goes down together (npm -> node -> python-gyp, etc.).
    """
    if not pids:
        return []

    # Send the signal. ``kill -SIG -PGID`` targets the whole process group.
    # We discover the PGID per-PID because some processes don't share a pgrp.
    for pid in pids:
        # Best-effort: discover the process group, then signal it.
        exit_code, output = await _exec(
            container,
            ['ps', '-o', 'pgid=', '-p', str(pid)],
        )
        pgid: int | None = None
        if exit_code == 0:
            line = output.strip().splitlines()[0] if output.strip() else ''
            try:
                pgid = int(line)
            except ValueError:
                pgid = None

        if pgid and pgid > 1:
            # Signal the whole group. The leading dash + pgid is the standard
            # kill(1) idiom for process-group signalling.
            await _exec(
                container,
                ['kill', f'-{signal_name}', f'-{pgid}'],
            )
        else:
            # Fall back to single-PID signal (still better than nothing).
            await _exec(
                container,
                ['kill', f'-{signal_name}', str(pid)],
            )

    # Poll for liveness until grace expires.
    deadline = asyncio.get_running_loop().time() + grace_seconds
    alive = list(pids)
    while alive and asyncio.get_running_loop().time() < deadline:
        alive = await _filter_alive(container, alive)
        if not alive:
            return []
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    return alive


async def _filter_alive(container, pids: list[int]) -> list[int]:
    """Return the subset of ``pids`` that still exists inside the container."""
    if not pids:
        return []
    # `kill -0` returns 0 if the PID exists and we have permission to signal it.
    # We invoke once per PID — they're cheap and the typical N is small (<10).
    alive: list[int] = []
    for pid in pids:
        exit_code, _ = await _exec(container, ['kill', '-0', str(pid)])
        if exit_code == 0:
            alive.append(pid)
    return alive


# --- helpers: docker exec wrapper -------------------------------------------


async def _exec(
    container,
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run a command inside ``container`` and capture (exit_code, stdout+stderr).

    The Docker SDK call is blocking, so we offload it to the default executor
    and apply a hard timeout to keep a stuck container from wedging Stop.

    ``env`` is layered on top of the container's default environment (used
    primarily to set ``TMUX_TMPDIR`` for tmux invocations).
    """
    loop = asyncio.get_running_loop()

    def _run() -> tuple[int, bytes]:
        # demux=False merges stdout+stderr — that's what we want for diagnostics.
        # We pass argv as a list (not a shell string) so we don't have to quote.
        exec_kwargs = dict(
            stdout=True,
            stderr=True,
            demux=False,
            tty=False,
            privileged=False,
        )
        if env:
            exec_kwargs['environment'] = env  # type: ignore[assignment]
        exit_code, output = container.exec_run(cmd, **exec_kwargs)
        if isinstance(output, tuple):  # demux=True path — defensive
            output = b''.join(part for part in output if part)
        return exit_code, output or b''

    try:
        exit_code, output_bytes = await asyncio.wait_for(
            loop.run_in_executor(None, _run),
            timeout=EXEC_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        _logger.warning(
            'docker exec timed out after %.1fs: %s',
            EXEC_TIMEOUT_SECONDS,
            shlex.join(cmd),
        )
        return 124, ''
    except APIError as exc:
        _logger.warning('docker exec failed: %s (cmd=%s)', exc, shlex.join(cmd))
        return 125, ''
    return exit_code, output_bytes.decode('utf-8', errors='replace')
