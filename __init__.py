"""topic-title-sync — mirror Hermes session titles onto Telegram supergroup forum topics.

Hermes already renames a topic when it auto-titles a session, but only for
*private-chat* topics (``gateway/run_topics.py::_is_telegram_topic_lane``, which
requires ``chat_type == "dm"``). Supergroup forum topics get session isolation but
never a rename, so a topic keeps whatever name an admin gave it.

This plugin closes that gap without touching core:

1. ``on_session_end`` fires once per turn with ``session_id`` / ``platform``.
   By then the auto-titler has written ``sessions.title``, so there is no race
   with the title generator (unlike ``post_auxiliary_call``).
2. The session row in ``state.db`` supplies the coordinates: ``chat_id``,
   ``thread_id``, ``chat_type`` and the title.
3. The rename goes through the sanctioned, capability-gated
   ``ctx.platform_actions.set_thread_title`` facade (audited, and it resolves the
   adapter for the active profile).
4. Renames are deduplicated per ``chat_id:thread_id`` in the plugin data dir, so a
   long conversation costs one ``editForumTopic`` per distinct title rather than
   one per turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

PLUGIN_ID = "topic-title-sync"
PLATFORM = "telegram"
#: Telegram group-ish chat types. The session row records what the gateway routed on
#: (``_normalize_chat_type`` yields ``forum`` when a thread_id is present) but plain
#: ``group`` / ``supergroup`` rows also show up, so accept all of them; the presence of a
#: non-General ``thread_id`` is what actually identifies a topic.
GROUP_CHAT_TYPES = frozenset({"forum", "group", "supergroup"})
#: Telegram's General topic: no ``thread_id``, or the pinned ``1``.
GENERAL_THREAD_IDS = frozenset({"", "none", "1"})
#: Keep the dedupe ledger bounded; oldest insertions are dropped first.
MAX_LEDGER_ENTRIES = 500

_state_lock = threading.Lock()
_db_lock = threading.Lock()
_db: Any = None
_capability_warned = False


# ── persisted dedupe ledger ──────────────────────────────────────────────────────────────


def _ledger_path():
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_ID) / "synced.json"


def _load_ledger() -> dict[str, str]:
    try:
        with open(_ledger_path(), encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception:
        logger.debug("%s: unreadable ledger, starting fresh", PLUGIN_ID, exc_info=True)
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_ledger(ledger: dict[str, str]) -> None:
    try:
        path = _ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if len(ledger) > MAX_LEDGER_ENTRIES:
            keep = list(ledger)[-MAX_LEDGER_ENTRIES:]
            ledger = {key: ledger[key] for key in keep}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        logger.debug("%s: could not persist ledger", PLUGIN_ID, exc_info=True)


# ── session lookup ──────────────────────────────────────────────────────────────────────


def _session_db() -> Any:
    """One read-only ``SessionDB`` for the process: no migrations, no writers."""
    global _db
    with _db_lock:
        if _db is None:
            from hermes_state import SessionDB

            _db = SessionDB(read_only=True)
        return _db


def _session_row(session_id: str) -> Optional[dict[str, Any]]:
    try:
        row = _session_db().get_session(session_id)
    except Exception:
        logger.debug("%s: session lookup failed for %s", PLUGIN_ID, session_id, exc_info=True)
        return None
    return row if isinstance(row, dict) else None


# ── cross-thread dispatch onto the gateway loop ─────────────────────────────────────────


def _gateway_loop() -> Optional[asyncio.AbstractEventLoop]:
    """The loop the delivery adapters live on (``GatewayRunner._gateway_loop``)."""
    try:
        from gateway.run import _gateway_runner_ref

        runner = _gateway_runner_ref()
    except Exception:
        return None
    loop = getattr(runner, "_gateway_loop", None) if runner is not None else None
    try:
        if loop is not None and not loop.is_closed():
            return loop
    except Exception:
        return None
    return None


def _finished(label: str, on_ok: Optional[Callable[[], None]], future: Any, *, takes_timeout: bool) -> None:
    """Shared completion handling for both the threaded and same-loop paths."""
    try:
        result = future.result(timeout=30) if takes_timeout else future.result()
    except Exception:
        logger.debug("%s: %s failed", PLUGIN_ID, label, exc_info=True)
        return
    if isinstance(result, dict) and result.get("ok") is False:
        logger.warning("%s: %s refused: %s", PLUGIN_ID, label, result.get("error"))
        return
    logger.info("%s: %s ok", PLUGIN_ID, label)
    if on_ok is not None:
        try:
            on_ok()
        except Exception:
            logger.debug("%s: post-rename bookkeeping failed", PLUGIN_ID, exc_info=True)


def _schedule(coro, label: str, on_ok: Optional[Callable[[], None]] = None) -> bool:
    """Run *coro* on the gateway loop; never block the caller (a hook thread)."""
    loop = _gateway_loop()
    if loop is not None:
        from agent.async_utils import safe_schedule_threadsafe

        future = safe_schedule_threadsafe(coro, loop, logger=logger, log_message=f"{label}: schedule failed")
        if future is None:
            return False
        future.add_done_callback(lambda fut: _finished(label, on_ok, fut, takes_timeout=True))
        return True
    # No gateway (a plain CLI session): only rename if some loop of ours can run it.
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        task = running.create_task(coro)
        task.add_done_callback(lambda fut: _finished(label, on_ok, fut, takes_timeout=False))
        return True
    coro.close()
    logger.debug("%s: no event loop available; skipping %s", PLUGIN_ID, label)
    return False


# ── the sync itself ────────────────────────────────────────────────────────────────────


def sync_session(ctx, session_id: str) -> None:
    """Rename the forum topic bound to *session_id* to that session's title."""
    row = _session_row(session_id)
    if not row:
        return
    if str(row.get("source") or PLATFORM) != PLATFORM:
        return
    if str(row.get("chat_type") or "") not in GROUP_CHAT_TYPES:
        return
    title = str(row.get("title") or "").strip()
    chat_id = str(row.get("chat_id") or "").strip()
    thread_id = str(row.get("thread_id") or "").strip()
    if not title or not chat_id or thread_id.lower() in GENERAL_THREAD_IDS:
        return

    key = f"{chat_id}:{thread_id}"
    if _load_ledger().get(key) == title:
        return

    global _capability_warned
    try:
        granted = ctx.has_capability("gateway.platform_actions")
    except Exception:
        granted = False
    if not granted:
        if not _capability_warned:
            _capability_warned = True
            logger.warning(
                "%s: the gateway.platform_actions capability is not granted; "
                "topic titles will not sync (grant it in `hermes plugins`)", PLUGIN_ID,
            )
        return

    def _remember() -> None:
        with _state_lock:
            ledger = _load_ledger()
            ledger[key] = title
            _save_ledger(ledger)

    label = f"rename topic {key} -> {title!r}"
    coro = ctx.platform_actions.set_thread_title(PLATFORM, chat_id, thread_id, title)
    if _schedule(coro, label, on_ok=_remember):
        logger.debug("%s: scheduled %s", PLUGIN_ID, label)


def register(ctx) -> None:
    def on_session_end(*, session_id: Optional[str] = None, platform: Optional[str] = None, **kwargs) -> None:
        # A turn end is the cheapest place to observe a title: the titler runs in the
        # turn prologue, so any title it wrote is already durable here.
        if not session_id or (platform or "").lower() != PLATFORM:
            return
        try:
            sync_session(ctx, session_id)
        except Exception:
            logger.debug("%s: sync failed for %s", PLUGIN_ID, session_id, exc_info=True)

    ctx.register_hook("on_session_end", on_session_end)
    logger.debug("%s: registered on_session_end", PLUGIN_ID)
