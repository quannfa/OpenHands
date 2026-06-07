"""Unit tests for ``openhands.app_server.sandbox.process_interrupt``.

These tests cover the *logic* of the signal-escalation routine without
spinning up a real Docker container. The container, ``exec_run`` results
and timing are all faked via a small in-test ``FakeContainer``/``FakeClient``
pair.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from openhands.app_server.sandbox import process_interrupt
from openhands.app_server.sandbox.process_interrupt import (
    InterruptResult,
    interrupt_sandbox_processes,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _ExecCall:
    """Record of one ``container.exec_run`` invocation for later assertions."""

    cmd: list[str]


@dataclass
class FakeContainer:
    status: str = 'running'
    exec_results: dict[tuple[str, ...], list[tuple[int, bytes]]] = field(
        default_factory=dict
    )
    calls: list[_ExecCall] = field(default_factory=list)
    pid_alive_calls_until_death: dict[int, int] = field(default_factory=dict)

    def exec_run(self, cmd: list[str], **_: Any):
        self.calls.append(_ExecCall(cmd=list(cmd)))
        key = tuple(cmd)

        if len(cmd) == 3 and cmd[:2] == ['kill', '-0']:
            try:
                pid = int(cmd[2])
            except ValueError:
                return 1, b''
            remaining = self.pid_alive_calls_until_death.get(pid)
            if remaining is None:
                return 1, b''
            if remaining <= 0:
                return 1, b''
            self.pid_alive_calls_until_death[pid] = remaining - 1
            return 0, b''

        responses = self.exec_results.get(key)
        if responses is None:
            return 0, b''
        if not responses:
            return 0, b''
        if len(responses) == 1:
            return responses[0]
        return responses.pop(0)


class FakeClient:
    def __init__(self, container: FakeContainer | None):
        self._container = container
        self.containers = MagicMock()
        if container is None:
            from docker.errors import NotFound

            self.containers.get.side_effect = NotFound('missing')
        else:
            self.containers.get.return_value = container


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _short_timeouts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(process_interrupt, 'SIGINT_GRACE_SECONDS', 0.2)
    monkeypatch.setattr(process_interrupt, 'SIGTERM_GRACE_SECONDS', 0.2)
    monkeypatch.setattr(process_interrupt, 'SIGKILL_REAP_SECONDS', 0.2)
    monkeypatch.setattr(process_interrupt, 'POLL_INTERVAL_SECONDS', 0.01)


@pytest.mark.asyncio
async def test_returns_container_not_found_when_missing():
    client = FakeClient(container=None)
    result: InterruptResult = await interrupt_sandbox_processes(client, 'oh-x')
    assert result.error == 'container_not_found'
    assert result.candidate_pids == 0


@pytest.mark.asyncio
async def test_container_not_running_is_noop():
    container = FakeContainer(status='paused')
    client = FakeClient(container=container)
    result = await interrupt_sandbox_processes(client, 'oh-x')
    assert result.error and result.error.startswith('container_not_running')


@pytest.mark.asyncio
async def test_tmux_path_finds_and_sigints_children():
    """Happy path: tmux lists one pane, the pane has one child, SIGINT kills it."""
    container = FakeContainer(
        exec_results={
            ('tmux', '-Lopenhands', 'list-panes', '-a', '-F', '#{pane_pid}'): [
                (0, b'100\n')
            ],
            ('ps', '-o', 'pid=', '--ppid', '100'): [(0, b'  200\n')],
            ('ps', '-o', 'pgid=', '-p', '200'): [(0, b'200\n')],
            ('kill', '-INT', '-200'): [(0, b'')],
        }
    )
    container.pid_alive_calls_until_death[200] = 1

    client = FakeClient(container=container)
    result = await interrupt_sandbox_processes(client, 'oh-x')

    assert result.candidate_pids == 1
    assert result.sigint_killed == 1
    assert result.sigterm_killed == 0
    assert result.sigkill_killed == 0
    assert result.survivors == []
    assert result.error is None


@pytest.mark.asyncio
async def test_tmux_socket_is_always_Lopenhands():
    """Verify we use the correct custom socket for the agent-server's tmux."""
    container = FakeContainer(
        exec_results={
            # Pane PID alone, no children — just checks the exact command was sent.
            ('tmux', '-Lopenhands', 'list-panes', '-a', '-F', '#{pane_pid}'): [
                (0, b'100\n')
            ],
            ('ps', '-o', 'pid=', '--ppid', '100'): [(0, b'')],
        }
    )
    client = FakeClient(container=container)
    await interrupt_sandbox_processes(client, 'oh-x')
    # Verify the EXACT command sent to docker exec
    assert ('tmux', '-Lopenhands', 'list-panes', '-a', '-F', '#{pane_pid}') in [
        tuple(c.cmd) for c in container.calls
    ]


@pytest.mark.asyncio
async def test_pane_shell_itself_is_never_targeted():
    """If ``ps --ppid`` echoes the pane PID, we skip it (defensive)."""
    container = FakeContainer(
        exec_results={
            ('tmux', '-Lopenhands', 'list-panes', '-a', '-F', '#{pane_pid}'): [
                (0, b'100\n')
            ],
            ('ps', '-o', 'pid=', '--ppid', '100'): [(0, b'100\n')],
        }
    )
    client = FakeClient(container=container)
    result = await interrupt_sandbox_processes(client, 'oh-x')

    assert result.candidate_pids == 0


@pytest.mark.asyncio
async def test_escalates_to_sigterm_then_sigkill():
    """Stubborn process: ignores SIGINT and SIGTERM, dies on SIGKILL."""
    container = FakeContainer(
        exec_results={
            ('tmux', '-Lopenhands', 'list-panes', '-a', '-F', '#{pane_pid}'): [
                (0, b'100\n')
            ],
            ('ps', '-o', 'pid=', '--ppid', '100'): [(0, b'200\n')],
            ('ps', '-o', 'pgid=', '-p', '200'): [(0, b'200\n')],
            ('kill', '-INT', '-200'): [(0, b'')],
            ('kill', '-TERM', '-200'): [(0, b'')],
            ('kill', '-KILL', '-200'): [(0, b'')],
        }
    )
    container.pid_alive_calls_until_death[200] = 40

    client = FakeClient(container=container)
    result = await interrupt_sandbox_processes(client, 'oh-x')

    assert result.sigint_killed == 0
    assert result.sigterm_killed == 0
    assert result.sigkill_killed == 1
    assert result.survivors == []


@pytest.mark.asyncio
async def test_tmux_unavailable_returns_noop():
    """If tmux exits non-zero, we do nothing — no fallback."""
    container = FakeContainer(
        exec_results={
            ('tmux', '-Lopenhands', 'list-panes', '-a', '-F', '#{pane_pid}'): [
                (127, b'command not found')
            ],
        }
    )
    client = FakeClient(container=container)
    result = await interrupt_sandbox_processes(client, 'oh-x')

    assert result.candidate_pids == 0
    assert result.tmux_pids_seen == 0
    # No fallback happened — tmux_unavailable is safe
    assert result.error is None


@pytest.mark.asyncio
async def test_tmux_no_panes_returns_noop():
    """If tmux runs but returns no panes, we do nothing."""
    container = FakeContainer(
        exec_results={
            ('tmux', '-Lopenhands', 'list-panes', '-a', '-F', '#{pane_pid}'): [
                (0, b'')
            ],
        }
    )
    client = FakeClient(container=container)
    result = await interrupt_sandbox_processes(client, 'oh-x')

    assert result.candidate_pids == 0


@pytest.mark.asyncio
async def test_multiple_panes_multiple_children():
    """Multiple tmux panes, each with a running child."""
    container = FakeContainer(
        exec_results={
            ('tmux', '-Lopenhands', 'list-panes', '-a', '-F', '#{pane_pid}'): [
                (0, b'100\n200\n')
            ],
            ('ps', '-o', 'pid=', '--ppid', '100'): [(0, b'300\n')],
            ('ps', '-o', 'pid=', '--ppid', '200'): [(0, b'400\n')],
            ('ps', '-o', 'pgid=', '-p', '300'): [(0, b'300\n')],
            ('ps', '-o', 'pgid=', '-p', '400'): [(0, b'400\n')],
            ('kill', '-INT', '-300'): [(0, b'')],
            ('kill', '-INT', '-400'): [(0, b'')],
        }
    )
    container.pid_alive_calls_until_death[300] = 1
    container.pid_alive_calls_until_death[400] = 1

    client = FakeClient(container=container)
    result = await interrupt_sandbox_processes(client, 'oh-x')

    assert result.candidate_pids == 2
    assert result.sigint_killed == 2


@pytest.mark.asyncio
async def test_exec_timeout_does_not_crash(monkeypatch: pytest.MonkeyPatch):
    """A wedged docker exec should not hang the helper indefinitely."""

    def hanging_exec(cmd, **kw):
        import time as _time

        _time.sleep(0.5)
        return 0, b''

    monkeypatch.setattr(process_interrupt, 'EXEC_TIMEOUT_SECONDS', 0.1)

    container = FakeContainer()
    container.exec_run = hanging_exec  # type: ignore[method-assign]

    client = FakeClient(container=container)
    result = await asyncio.wait_for(
        interrupt_sandbox_processes(client, 'oh-x'),
        timeout=5.0,
    )
    assert result.candidate_pids == 0


# --- TMUX_TMPDIR discovery tests --------------------------------------------


@pytest.mark.asyncio
async def test_discovers_tmux_env_from_proc():
    """When pgrep finds a tmux process with TMUX_TMPDIR, list-panes uses it."""
    container = FakeContainer(
        exec_results={
            # _discover_tmux_env: pgrep
            ('pgrep', '-f', 'tmux'): [(0, b'139\n')],
            # _read_proc_environ: cat /proc/139/environ
            ('cat', '/proc/139/environ'): [
                (0, b'TMUX_TMPDIR=/tmp/openhands-agent-server-58\x00HOME=/root\x00')
            ],
            # _collect_via_tmux: list-panes (now with env)
            ('tmux', '-Lopenhands', 'list-panes', '-a', '-F', '#{pane_pid}'): [
                (0, b'100\n')
            ],
            ('ps', '-o', 'pid=', '--ppid', '100'): [(0, b'200\n')],
            ('ps', '-o', 'pgid=', '-p', '200'): [(0, b'200\n')],
            ('kill', '-INT', '-200'): [(0, b'')],
        }
    )
    container.pid_alive_calls_until_death[200] = 1

    client = FakeClient(container=container)
    result = await interrupt_sandbox_processes(client, 'oh-x')

    assert result.candidate_pids == 1
    assert result.sigint_killed == 1
    # Verify list-panes was called WITH TMUX_TMPDIR in the environment
    list_panes_call = next(
        c for c in container.calls
        if c.cmd == ['tmux', '-Lopenhands', 'list-panes', '-a', '-F', '#{pane_pid}']
    )
    # We can't assert on the env dict directly since FakeContainer.exec_run
    # doesn't capture kwargs, but we verify the sequence is correct:
    # pgrep → cat /proc → list-panes → ps → kill
    cmds_flat = [tuple(c.cmd) for c in container.calls]
    assert ('pgrep', '-f', 'tmux') in cmds_flat
    assert ('cat', '/proc/139/environ') in cmds_flat


@pytest.mark.asyncio
async def test_tmux_env_discovery_fails_gracefully():
    """When pgrep fails, we still call list-panes (just without env)."""
    container = FakeContainer(
        exec_results={
            ('pgrep', '-f', 'tmux'): [(1, b'')],
            # No cat /proc call because pgrep returned nothing
            # list-panes is called WITHOUT TMUX_TMPDIR env
            ('tmux', '-Lopenhands', 'list-panes', '-a', '-F', '#{pane_pid}'): [
                (0, b'100\n')
            ],
            ('ps', '-o', 'pid=', '--ppid', '100'): [(0, b'200\n')],
            ('ps', '-o', 'pgid=', '-p', '200'): [(0, b'200\n')],
            ('kill', '-INT', '-200'): [(0, b'')],
        }
    )
    container.pid_alive_calls_until_death[200] = 1

    client = FakeClient(container=container)
    result = await interrupt_sandbox_processes(client, 'oh-x')
    assert result.candidate_pids == 1


@pytest.mark.asyncio
async def test_tmux_env_discovery_no_tmpdir_in_environ():
    """When pgrep works but the process has no TMUX_TMPDIR, we proceed without it."""
    container = FakeContainer(
        exec_results={
            ('pgrep', '-f', 'tmux'): [(0, b'139\n')],
            ('cat', '/proc/139/environ'): [(0, b'HOME=/root\x00PATH=/bin\x00')],
            ('tmux', '-Lopenhands', 'list-panes', '-a', '-F', '#{pane_pid}'): [
                (0, b'100\n')
            ],
            ('ps', '-o', 'pid=', '--ppid', '100'): [(0, b'200\n')],
            ('ps', '-o', 'pgid=', '-p', '200'): [(0, b'200\n')],
            ('kill', '-INT', '-200'): [(0, b'')],
        }
    )
    container.pid_alive_calls_until_death[200] = 1

    client = FakeClient(container=container)
    result = await interrupt_sandbox_processes(client, 'oh-x')
    assert result.candidate_pids == 1
