"""Publish-gate helpers: deterministic comment order for concurrently published tools.

The gate is a cross-process signal file. A "signaler" writes it after publishing; a
"waiter" blocks on it before publishing and gives up after a timeout so a crashed peer
cannot deadlock the other tool.
"""
import threading
import time
from pathlib import Path

from pr_agent.algo.utils import signal_publish_gate, wait_on_publish_gate
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

KEYS = [
    "config.publish_gate_file",
    "config.publish_gate_role",
    "config.publish_gate_timeout_seconds",
]


def _configure(gate_path: Path, role: str, timeout: float = 5) -> None:
    get_settings().set("config.publish_gate_file", str(gate_path))
    get_settings().set("config.publish_gate_role", role)
    get_settings().set("config.publish_gate_timeout_seconds", timeout)


def test_no_op_when_unconfigured(tmp_path):
    """With no gate file configured both helpers return immediately."""
    snapshot = snapshot_settings(KEYS)
    try:
        _configure(tmp_path / "gate", "signaler")
        get_settings().set("config.publish_gate_file", "")
        signal_publish_gate()
        assert not (tmp_path / "gate").exists()

        start = time.monotonic()
        wait_on_publish_gate()
        assert time.monotonic() - start < 1, "waiter must not block when the gate is disabled"
    finally:
        restore_settings(snapshot)


def test_signaler_writes_the_gate_file(tmp_path):
    snapshot = snapshot_settings(KEYS)
    try:
        gate = tmp_path / "gate"
        _configure(gate, "signaler")
        signal_publish_gate()
        assert gate.exists()
    finally:
        restore_settings(snapshot)


def test_waiter_is_released_by_the_signaler(tmp_path):
    """The waiter returns as soon as the gate file appears, not at the timeout."""
    snapshot = snapshot_settings(KEYS)
    try:
        gate = tmp_path / "gate"
        _configure(gate, "waiter", timeout=10)
        threading.Timer(0.3, lambda: gate.write_text("published")).start()

        start = time.monotonic()
        wait_on_publish_gate()
        elapsed = time.monotonic() - start

        assert elapsed >= 0.3, "waiter must actually block until the signal"
        assert elapsed < 5, "waiter must return on the signal, not wait out the timeout"
    finally:
        restore_settings(snapshot)


def test_waiter_publishes_anyway_after_timeout(tmp_path):
    """A peer that never signals must not deadlock the waiter."""
    snapshot = snapshot_settings(KEYS)
    try:
        gate = tmp_path / "gate"
        _configure(gate, "waiter", timeout=1)

        start = time.monotonic()
        wait_on_publish_gate()
        elapsed = time.monotonic() - start

        assert elapsed >= 1, "waiter should have waited out the timeout"
        assert elapsed < 5
        assert not gate.exists()
    finally:
        restore_settings(snapshot)


def test_role_is_required(tmp_path):
    """Only an explicit role acts; an empty role is a no-op for both helpers."""
    snapshot = snapshot_settings(KEYS)
    try:
        gate = tmp_path / "gate"
        _configure(gate, "")
        signal_publish_gate()
        assert not gate.exists()

        start = time.monotonic()
        wait_on_publish_gate()
        assert time.monotonic() - start < 1
    finally:
        restore_settings(snapshot)
