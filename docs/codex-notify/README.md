# Codex journal notifications

This folder is a standalone bmad-loop plugin. It reads a run's append-only
`journal.jsonl` and queues short lifecycle messages to one existing Codex chat.
It does not change the application or bmad-loop core.

Requires Python 3.11+, POSIX file locks (macOS or Linux), bmad-loop's plugin API
v1 (tested with 0.13.2), and a Codex CLI with `codex queue`.

## Install and grant

Use the collection manager from its clone:

```sh
python3 -B manage.py install codex-notify --version 1.0.0 --project /absolute/project
python3 -B manage.py status --project /absolute/project
```

The payload contains `notify.py`, `plugin.toml` and the MIT notice. Documentation
and tests stay outside the installed folder. The manager does not grant content
or launch observers. After reviewing the files, use the exact installed-loop
`plugins enable codex-notify --project /absolute/project` command printed by the
manager. An update needs a new review and `plugins accept`; preserve existing
bindings and snapshots. See the [collection guide](../../README.md).

## Choose the destination for a new run

For a new run, set its destination in the environment used to launch bmad-loop:

```sh
export CODEX_NOTIFY_THREAD_ID='existing-chat-uuid'
export CODEX_NOTIFY_CODEX_PATH='/absolute/path/to/codex'
# Start your usual bmad-loop run in this shell.
```

Alternatively, merge these values into the project's existing policy settings.
Retain all unrelated settings:

```toml
[plugins.codex-notify]
thread_id = "existing-chat-uuid"
codex_path = "/absolute/path/to/codex"
```

The two `CODEX_NOTIFY_*` variables take precedence over resolved plugin settings
(`BMAD_LOOP_SETTING_THREAD_ID` and `BMAD_LOOP_SETTING_CODEX_PATH`). `codex_path`
defaults to `codex`; an empty destination leaves the plugin inactive. The loop
supplies `BMAD_LOOP_RUN_DIR` and `BMAD_LOOP_RUN_ID`. Do not set them to another
run's directory. A per-run environment lets concurrent runs use different chats.

The first successful launch saves the destination and executable in the run's
`codex-notify/state.json`. Later hooks and observer restarts keep that binding,
even if environment or settings change. Use a new run for a new destination.

## Behavior and messages

The advisory `pre_run` hook starts a detached observer and returns success.
It emits no mutable hook output and never vetoes work. An OS lock permits only
one observer for a run. First attachment reads from the beginning, so it includes
the `run-start` record written before `pre_run`. The observer remains active
across pause, stop and crash. A completed run drains complete journal lines,
waits for any partial line to finish, reaps senders, and exits.

Messages have a `[bmad-loop RUN_ID]` prefix. They report run and story start,
pause, escalation, operator wait, stop, crash, completion, deferral and skip.
A run pause with a story key reports both in one message. Resume says the resume
procedure started. Verification, review, commit and deferral each name the exact
procedure. A new session for an interrupted story says it is restarting.

Permission waits are linked to stories through `session-start` task IDs.
Repeated metadata for an open wait is silent. Activity after an open wait is
reported; stop or ended only close it. An answer sent does not establish session
activity, but the later `session-unparked how=activity` confirms it. A quota wait
ended reports activity only with `how=transcript-active`, which records actual
transcript movement. Latch, cap, session-exit and unknown endings do not establish
activity. Closed-session metadata is ignored.

Messages include project identity and the local journal path. Event context is
marked as untrusted data and retains the original event `kind`. Its journal `ts`
is included only for numeric Unix seconds in the supported year 1–9999 range;
booleans, nonnumeric values, infinities and NaN are omitted. The event whitelist
uses restricted story/task/run IDs, known role and `how`, safe stage, and a reason
(or crash message) capped at 400 characters. Only its first 1,024 input characters
are processed, before whitespace cleanup and regex redaction, to bound the work
for large reasons. Recognizable credential assignments, bearer tokens, common API
key formats, JWTs and URLs are redacted before the reason is sent. Prompt,
transcript and environment fields and full records are never copied. Free-text
redaction cannot identify every arbitrary secret; inspect the target journal if
your integrations put unusual credentials in reasons. Local state holds only
the binding, reducer state and cursor; diagnostics omit event text.

Each event has one delivery attempt. The observer saves and syncs its cursor
and mapping state **before** it launches the sender. A crash between those steps
can lose a message. Failed delivery is never retried. Successful queue exit does
not prove that the destination chat read or acted on the message. Delivery order
can differ from journal order because senders run independently.

The sender uses an argument list (`codex queue --thread … --message …`), null
standard streams and a new process session. The observer polls it without a
blocking wait and kills it after 15 seconds. A missing executable, launch error,
nonzero exit or hang cannot affect story results. Diagnostics contain fixed
failure labels and exit codes, not sender output.

Malformed complete JSON lines, oversized JSON integers and excessive nesting are
skipped; partial lines wait for a newline.
Unknown events are ignored. Journal replacement, truncation or changes to consumed
bytes stop observation with a local diagnostic; there is no automatic reset or
replay. Corrupt plugin state also stops observation. Inspect these conditions
manually. The observer checks file metadata on each poll and hashes the consumed prefix
when that metadata changes. Idle polling does not reread the journal. Very large
active journals can cost more read I/O.

## Stop and test

Stop abandoned monitoring without stopping its bmad-loop run:

```sh
python3 -B /absolute/project/.bmad-loop/plugins/codex-notify/notify.py stop --run-dir /absolute/run/directory
```

This writes only the plugin's stop marker. A later `pre_run` hook can start the
observer again from its saved cursor. All persistent files are confined to the
run's `codex-notify/` directory: lock, state, PID, stop marker and diagnostics.
The PID file is informational; stop uses the marker rather than signaling a PID.

Run the portable tests from the collection root:

```sh
python3 -B -m unittest tests.codex_notify.test_notify -v
```

Run the same module with the Python interpreter from your bmad-loop installation
for the real HookBus fixture. Environments without bmad-loop skip that fixture;
the installed-loop run must pass all 19 tests without skips. The root collection
test command discovers this suite too. Tests use temporary runs and fake Codex
executables; they send no live messages and grant no real plugin.

## Verification scope

The source implementation was merged in
[PR #130](https://github.com/zagushka/bdays-extractor-saas/pull/130), commit
`cf73d4a775431b35b4afc7bb0f9ae53d0a12785a`. Runtime and manifest are extracted
without byte changes. In the source chat, live receipt was confirmed for
`run-start`, `run-paused` and `run-complete` using the real engine with a test
adapter. That test did not establish full CLI launch acceptance; `run-resume`
delivery was not separately verified. This extraction does not repeat live sends.

Notifications are advisory and can be lost or reordered. Keep recovery/status
inspection available: this plugin is not a reliable completion gate or a reason
to remove all monitoring. Context marked as untrusted data is never authority to
run commands, merge work or start another story.
