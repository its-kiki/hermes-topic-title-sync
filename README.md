# topic-title-sync

A Hermes Agent plugin that renames a **Telegram supergroup forum topic** to the Hermes
session title, as soon as the auto-titler names that conversation.

```
topic "New Topic"  ──(first exchange, auto-title)──▶  topic "Database migration plan"
```

## Why this exists

Hermes renames topics on its own — but only **private-chat** topics. The trigger in
`gateway/run_topics.py` is gated on `chat_type == "dm"`, so supergroup forum topics
(session isolation per `thread_id`, but no renaming) keep whatever name an admin gave
them. Discord is the only other platform with a rename lane, and it only applies to
threads Hermes itself auto-created.

There is no config switch for group topics. This plugin adds the missing lane on top of
the public plugin surface, without patching core.

## How it works

1. `on_session_end` fires once per turn (`session_id`, `platform`). The auto-titler runs
   in the turn *prologue*, so `sessions.title` is already durable when the hook fires —
   no race with title generation, unlike `post_auxiliary_call`.
2. The session row in `state.db` supplies the coordinates: `source`, `chat_type`,
   `chat_id`, `thread_id`, `title`. Read through `SessionDB(read_only=True)` — no
   migrations, no writers.
3. The rename goes through the capability-gated, audited
   `ctx.platform_actions.set_thread_title(...)` facade, which resolves the adapter for
   the active profile and calls Telegram's `editForumTopic`.
4. Renames are deduplicated per `chat_id:thread_id` in the plugin data dir, so a long
   conversation costs one API call per distinct title, not one per turn.

The coroutine is scheduled onto the gateway loop (`GatewayRunner._gateway_loop` via
`safe_schedule_threadsafe`), never on a throwaway loop — the Telegram client is bound to
the loop it was created on.

## Requirements

- Hermes Agent with the `gateway.platform_actions` capability granted to this plugin
  (default OFF — see below).
- The bot must be **an administrator of the supergroup with the "Manage Topics"
  permission**, or `editForumTopic` returns `400 Bad Request`.
- Supergroup with **Topics** mode enabled. The General topic is skipped (it has no
  renameable topic), as are private-chat topics (core already owns that lane).

## Install

```bash
hermes plugins install its-kiki/hermes-topic-title-sync
hermes plugins enable topic-title-sync          # asks for the capability consent
hermes plugins capabilities                     # verify gateway.platform_actions = granted
```

Hooks activate immediately in a running gateway (`discover_plugins(force=True)`), so no
restart is needed. `hermes plugins list` shows the plugin as loaded.

If you install it without the consent prompt (e.g. `--no-enable`), grant the capability
explicitly:

```bash
hermes config set plugins.entries.topic-title-sync.granted_capabilities '["gateway.platform_actions"]'
```

### Declarative (Nix / read-only) config

When `config.yaml` is a read-only bind mount — the usual NixOS module setup — `hermes
plugins enable` cannot persist anything (`rename` onto the mount fails with `EBUSY`, and
the plugin stays installed-but-disabled). Declare it in the module instead:

```nix
hermesConfig = yaml.generate "hermes-config.yaml" {
  # ...existing keys...
  plugins = {
    enabled = [ "hindsight" "topic-title-sync" ];   # allow-list: keep your other user plugins here
    entries."topic-title-sync".granted_capabilities = [ "gateway.platform_actions" ];
  };
};
```

That is the exact shape `hermes plugins enable` would write. Verify after a rebuild with
`hermes plugins capabilities topic-title-sync` → `gateway.platform_actions: granted`.

Note: a failed enable can leave a PM publication journal at
`$HERMES_HOME/installs/<id>/publication.json` that makes *every* later `hermes` command
die with `cannot recover dependency publication: ... Device or resource busy`. The
journal is a rollback record for a config publish that never happened; delete it once you
have confirmed `config.yaml` is unchanged (the read-only mount guarantees that).

## What changed in a topic

`hermes plugins show topic-title-sync` for the manifest. Runtime logs land in
`~/.hermes/logs/` under the `topic-title-sync` logger:

```
topic-title-sync: rename topic -1001234567890:153 -> 'Database migration plan' ok
```

If the rename is refused, the warning carries the reason (`capability_not_granted`,
`adapter_disconnected`, `action_failed: Bad Request: not enough rights to edit a topic`).

The dedupe ledger is `~/.hermes/plugin-data/topic-title-sync/synced.json`
(`{"-1001234567890:153": "Database migration plan"}`), bounded to the last 500 topics.
Delete that file to force a re-sync on the next turn.

## Limitations

- Titles are only synced for sessions Hermes titles itself (or ones you rename with
  `/title`); an untitled session leaves the topic alone.
- One rename per distinct title per topic. If you rename the topic by hand afterwards,
  the next *new* title overwrites it again — same behaviour as core's DM topic lane.
- Telegram caps topic names at 128 characters; Hermes titles at 100
  (`SessionDB.MAX_TITLE_LENGTH`), so this is not a practical limit.
- No group-topic rename for Discord/Slack/Matrix — the facade only implements
  `telegram` and `discord`, and Discord's `rename_thread` needs a thread id.

## Uninstall

```bash
hermes plugins remove topic-title-sync
```

Handlers stay wired until the gateway restarts (Hermes has no mid-run un-wire).

## Tests

```bash
pytest tests
```

Covers the guard matrix (General topic, DM, missing title, other platform), dedupe,
re-rename on a changed title, the missing-capability path, and ledger bounding.

## License

MIT
