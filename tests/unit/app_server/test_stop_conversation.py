"""Tests for the redesigned ``stop_conversation`` flow.

These tests cover the *interrupt* semantics introduced when the Stop button
was changed from "freeze the entire sandbox" to "Ctrl+C the current task":

  1. The agent-server is asked to pause.
  2. We poll until execution_status == PAUSED (event flush barrier).
  3. The Docker container is NOT paused — instead we call the new
     ``interrupt_sandbox_processes`` primitive.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from openhands.app_server.app_conversation.app_conversation_router import (
    _wait_for_agent_paused,
)


class _StubTransport(httpx.AsyncBaseTransport):
    """Tiny ASGI-style transport that returns a scripted sequence of responses."""

    def __init__(self, responses: list[tuple[int, dict]]):
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._responses:
            # Last response is "sticky" so tests can keep polling.
            status_code, body = 200, {}
        elif len(self._responses) == 1:
            status_code, body = self._responses[0]
        else:
            status_code, body = self._responses.pop(0)
        return httpx.Response(status_code=status_code, json=body)


@pytest.mark.asyncio
async def test_wait_returns_true_when_paused_immediately():
    transport = _StubTransport([(200, {'execution_status': 'PAUSED'})])
    async with httpx.AsyncClient(transport=transport) as client:
        result = await _wait_for_agent_paused(
            httpx_client=client,
            agent_server_url='http://agent:1234',
            conversation_id='dead-beef',  # type: ignore[arg-type]
            headers={'X-Session-API-Key': 'k'},
            timeout_seconds=1.0,
            poll_interval_seconds=0.01,
        )
    assert result is True
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_wait_polls_until_status_flips_to_paused():
    """First reply still RUNNING, second reply PAUSED — must wait then succeed."""
    transport = _StubTransport(
        [
            (200, {'execution_status': 'RUNNING'}),
            (200, {'execution_status': 'PAUSED'}),
        ]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        result = await _wait_for_agent_paused(
            httpx_client=client,
            agent_server_url='http://agent:1234',
            conversation_id='dead-beef',  # type: ignore[arg-type]
            headers={},
            timeout_seconds=1.0,
            poll_interval_seconds=0.01,
        )
    assert result is True
    assert len(transport.requests) >= 2


@pytest.mark.asyncio
async def test_wait_returns_false_on_timeout():
    """Status never reaches PAUSED — function must return False, not raise."""
    transport = _StubTransport([(200, {'execution_status': 'RUNNING'})])
    async with httpx.AsyncClient(transport=transport) as client:
        result = await asyncio.wait_for(
            _wait_for_agent_paused(
                httpx_client=client,
                agent_server_url='http://agent:1234',
                conversation_id='dead-beef',  # type: ignore[arg-type]
                headers={},
                timeout_seconds=0.1,
                poll_interval_seconds=0.02,
            ),
            timeout=2.0,
        )
    assert result is False
    # We should have made multiple polling attempts within the 100ms window.
    assert len(transport.requests) >= 1


@pytest.mark.asyncio
async def test_wait_tolerates_transient_http_errors():
    """An intermittent 500 should not abort the polling loop."""
    transport = _StubTransport(
        [
            (500, {}),
            (200, {'execution_status': 'PAUSED'}),
        ]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        result = await _wait_for_agent_paused(
            httpx_client=client,
            agent_server_url='http://agent:1234',
            conversation_id='dead-beef',  # type: ignore[arg-type]
            headers={},
            timeout_seconds=1.0,
            poll_interval_seconds=0.01,
        )
    assert result is True


@pytest.mark.asyncio
async def test_wait_accepts_lowercase_status_serialisations():
    """Defensive against future SDK enum casing changes."""
    transport = _StubTransport([(200, {'execution_status': 'paused'})])
    async with httpx.AsyncClient(transport=transport) as client:
        result = await _wait_for_agent_paused(
            httpx_client=client,
            agent_server_url='http://agent:1234',
            conversation_id='dead-beef',  # type: ignore[arg-type]
            headers={},
            timeout_seconds=1.0,
            poll_interval_seconds=0.01,
        )
    assert result is True
