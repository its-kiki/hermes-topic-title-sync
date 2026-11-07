"""Guard tests for topic-title-sync. Run with `python -m pytest tests`."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("topic_title_sync_under_test", ROOT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeActions:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"ok": True}

    async def set_thread_title(self, platform, chat_id, thread_id, title):
        self.calls.append((platform, chat_id, thread_id, title))
        return self.result


class FakeCtx:
    def __init__(self, actions, granted=True):
        self.platform_actions = actions
        self.granted = granted
        self.probes = []

    def has_capability(self, capability):
        self.probes.append(capability)
        return self.granted


@pytest.fixture()
def plugin(tmp_path, monkeypatch):
    module = _load()
    monkeypatch.setattr(module, "_ledger_path", lambda: tmp_path / "synced.json")
    calls = []

    def fake_schedule(coro, label, on_ok=None):
        # Run the rename coroutine for real so the fake actions record the call;
        # the asyncio/thread handoff itself was covered by a live probe.
        result = asyncio.run(coro)
        calls.append(label)
        if on_ok is not None and not (isinstance(result, dict) and result.get("ok") is False):
            on_ok()
        return True

    monkeypatch.setattr(module, "_schedule", fake_schedule)
    module.scheduled = calls
    return module


def _row(**overrides):
    row = {
        "id": "sess-1",
        "source": "telegram",
        "chat_type": "group",
        "chat_id": "-100123",
        "thread_id": "153",
        "title": "Database migration plan",
    }
    row.update(overrides)
    return row


def test_renames_group_topic(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_session_row", lambda sid: _row())
    actions = FakeActions()
    plugin.sync_session(FakeCtx(actions), "sess-1")
    assert actions.calls == [("telegram", "-100123", "153", "Database migration plan")]


def test_forum_chat_type_also_renames(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_session_row", lambda sid: _row(chat_type="forum"))
    actions = FakeActions()
    plugin.sync_session(FakeCtx(actions), "sess-1")
    assert len(actions.calls) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"thread_id": "1"},          # General topic
        {"thread_id": None},         # no thread at all
        {"title": None},             # untitled session
        {"title": "   "},            # whitespace only
        {"chat_type": "dm"},         # private-chat topic: core owns that lane
        {"source": "discord"},       # different platform
        {"chat_id": None},
    ],
)
def test_skips_non_qualifying_rows(plugin, monkeypatch, overrides):
    monkeypatch.setattr(plugin, "_session_row", lambda sid: _row(**overrides))
    actions = FakeActions()
    plugin.sync_session(FakeCtx(actions), "sess-1")
    assert actions.calls == []


def test_missing_session_row_is_ignored(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_session_row", lambda sid: None)
    actions = FakeActions()
    plugin.sync_session(FakeCtx(actions), "gone")
    assert actions.calls == []


def test_dedupes_per_topic(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_session_row", lambda sid: _row())
    actions = FakeActions()
    ctx = FakeCtx(actions)
    plugin.sync_session(ctx, "sess-1")
    plugin.sync_session(ctx, "sess-1")
    assert len(actions.calls) == 1


def test_new_title_renames_again(plugin, monkeypatch):
    actions = FakeActions()
    ctx = FakeCtx(actions)
    monkeypatch.setattr(plugin, "_session_row", lambda sid: _row())
    plugin.sync_session(ctx, "sess-1")
    monkeypatch.setattr(plugin, "_session_row", lambda sid: _row(title="Renamed later"))
    plugin.sync_session(ctx, "sess-1")
    assert [call[3] for call in actions.calls] == ["Database migration plan", "Renamed later"]


def test_skips_when_capability_not_granted(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_session_row", lambda sid: _row())
    actions = FakeActions()
    ctx = FakeCtx(actions, granted=False)
    plugin.sync_session(ctx, "sess-1")
    assert actions.calls == []
    assert ctx.probes == ["gateway.platform_actions"]


def test_ledger_stays_bounded(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_session_row", lambda sid: _row())
    plugin.MAX_LEDGER_ENTRIES = 3
    actions = FakeActions()
    ctx = FakeCtx(actions)
    for n in range(6):
        monkeypatch.setattr(plugin, "_session_row", lambda sid, n=n: _row(thread_id=str(100 + n), title=f"t{n}"))
        plugin.sync_session(ctx, "sess-1")
    assert len(plugin._load_ledger()) == 3
    assert list(plugin._load_ledger()) == ["-100123:103", "-100123:104", "-100123:105"]
