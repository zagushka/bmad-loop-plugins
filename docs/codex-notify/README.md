# Codex lifecycle notifications

**Deprecated:** use [status-notify](../status-notify/README.md) for new runs after
its release. Existing runtime versions, tags, grants, snapshots and saved bindings
remain unchanged. Follow the [transition guide](../status-notify/README.md#transition-from-deprecated-codex-notify)
between runs; do not enable both plugins for the same run.

This plugin queues short hook and journal messages to one existing Codex chat
per run. The journal observer reads the run's append-only `journal.jsonl`. It
does not change the application or bmad-loop core.

Requires Python 3.11+, POSIX file locks (macOS or Linux), bmad-loop's plugin API
v1 (tested with 0.13.2), and a Codex CLI with `codex queue`.

## Version 1.1.0

Version 1.1.0 adds notifications for all 49 hooks emitted by bmad-loop 0.13.2
and a `hooks` setting. Review the changed code and settings before updating
between runs. Existing 1.0.0 bindings remain journal-only. Installation and
content grants are separate steps.

## Install and grant

Use the collection manager from its clone:

```sh
python3 -B manage.py install codex-notify --version 1.1.0 --project /absolute/project
python3 -B manage.py status --project /absolute/project
```

The payload contains `notify.py`, `plugin.toml` and the MIT notice.
Documentation and tests stay outside the installed folder. The manager does not
grant content or launch observers. After reviewing the files, use the exact
installed-loop `plugins enable codex-notify --project /absolute/project` command
printed by the manager. An update needs a new review and `plugins accept`;
preserve existing bindings and snapshots. See the [collection
guide](../../README.md).

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
hooks = "pre_commit,pre_integrate,pre_merge,post_merge,post_story"
```

The two `CODEX_NOTIFY_*` variables take precedence over resolved plugin settings
(`BMAD_LOOP_SETTING_THREAD_ID` and `BMAD_LOOP_SETTING_CODEX_PATH`). `codex_path`
defaults to `codex`; an empty destination leaves the plugin inactive. The loop
supplies `BMAD_LOOP_RUN_DIR` and `BMAD_LOOP_RUN_ID`. Do not set them to another
run's directory. A per-run environment lets concurrent runs use different chats.

The first successful launch saves the destination, executable and hook selection
in the run's `codex-notify/state.json`. Later hooks and observer restarts keep
that binding, even if environment or settings change. Use a new run for a new
destination.

## Select hook notifications

`hooks` is a string because API 1 settings support scalar values. Use exact,
case-sensitive names separated by commas. Spaces around names are allowed and
duplicates count once:

| Value | Hook messages for a new run |
| --- | --- |
| Omitted or `"*"` | All 49 hooks listed below. |
| `"pre_commit,post_story"` | Only those two hook invocations. |
| `""` or whitespace | No hook messages. Journal messages remain active. |

An unknown name, empty item, `"*,pre_commit"`, or non-string value is invalid.
The plugin does not create a new binding or start notifications for an invalid
selection. Loop work is not vetoed. Diagnostic text does not echo the input.
There is no `CODEX_NOTIFY_HOOKS` override. The SDK supplies resolved settings.

Selection is fixed when a new run first saves its binding. Later policy or
environment changes do not change that run. Saved 1.0.0 bindings have no `hooks`
field and stay **journal-only**, without a state migration or rewrite. Existing
`thread_id`, `codex_path` and their environment overrides retain their meaning.
For new runs that use existing settings without `hooks`, the new default adds
all hook notifications. Use `hooks = ""` to retain journal-only behavior in new
runs.

The following set comes from source inspection of installed 0.13.2/API 1:

| Area | Emitted hooks |
| --- | --- |
| Run and selection | `pre_run`, `post_run`, `pre_pick_next`, `post_pick_next` |
| Story and ready gate | `pre_story`, `post_story`, `pre_ready_gate`, `post_ready_gate` |
| Worktree | `pre_worktree_setup`, `post_worktree_setup`, `pre_worktree_teardown`, `post_worktree_teardown` |
| Dev | `pre_dev_phase`, `post_dev_phase`, `post_dev_verify` |
| Review | `pre_review_phase`, `post_review_session`, `post_review_result`, `post_review_verify` |
| Sessions | `pre_session`, `post_session`, `pre_dev_session`, `pre_review_session`, `pre_triage_session`, `pre_retro_session`, `pre_fix_session`, `pre_migrate_session`, `pre_workflow_session` |
| Commit and integration | `pre_commit`, `post_commit`, `pre_integrate`, `pre_merge`, `post_merge` |
| Rollback | `pre_rollback`, `post_rollback` |
| Epic | `pre_epic_boundary`, `post_epic_boundary` |
| Sweep | `pre_bundle`, `post_bundle`, `pre_sweep_cycle`, `post_sweep_cycle`, `post_migrate`, `post_triage`, `pre_close_resolved`, `post_close_resolved`, `pre_decision`, `post_decision`, `pre_materialize_bundles`, `post_materialize_bundles` |

A hook message means **the hook was invoked**, not that its operation succeeded.
Only hooks reached on the run's actual path can send messages. `pre_commit_gate`
is a workflow injection stage, not an emitted plugin hook. `spec-approval` is a
journal pause stage, not a hook; its journal notification remains active for any
hook selection. Worktree teardown hooks are adjacent before restoration; they do
not bracket physical deletion. `post_run` is not emitted on every error, pause
or hard stop. Use journal messages and status inspection for those cases.
Repeated invocations, including replayed `post_migrate`, may repeat messages;
`delivery_id` is included when present, but the plugin does not deduplicate
hooks.

## Behavior and messages

An API-1 in-process bridge registers all 49 hooks. It uses the actual dispatch
name and read-only context, including the distinct `pre_session` dispatch that
shares a role-specific context in Loop. It does not infer hooks from journal
records or inherited `BMAD_LOOP_*` fields. `pre_run` starts the detached
observer; each selected hook starts a separate detached delivery process. The
callback performs only context formatting and process launch. It does no run-
file I/O and never waits for Codex. A separate declarative `pre_run` launcher
creates the binding and observer under the existing five-second timeout.
Delivery processes read the saved binding and selection. A missing binding is
silent. It never changes context fields, shared state, decisions or vetoes.
Errors are advisory. A bridge process-launch failure is silent. Launcher and
delivery diagnostics contain only fixed labels and exception type names. This
bridge runs under the normal content grant; an update does not grant itself. An
OS lock permits only one observer for a run. First attachment reads from the
beginning, so it includes the `run-start` record written before `pre_run`. The
observer remains active across pause, stop and crash. A completed run drains
complete journal lines, waits for any partial line to finish, reaps senders, and
exits.

Hook messages have `kind="hook"`, the exact stage, run ID and available
restricted story, phase, role, delivery ID and rollback outcome. They do not
copy free-text reasons, prompts, results, command output, environments or
worktree paths. The message states that the hook was invoked. Hook delivery is
independent of the journal observer and can complete after it exits or stops.

Messages have a `[bmad-loop RUN_ID]` prefix. Journal messages report run and
story start, pause, escalation, operator wait, stop, crash, completion, deferral
and skip. A run pause with a story key reports both in one message. Resume says
the resume procedure started. Verification, review, commit and deferral each
name the exact procedure. A new session for an interrupted story says it is
restarting.

Permission waits are linked to stories through `session-start` task IDs.
Repeated metadata for an open wait is silent. Activity after an open wait is
reported; stop or ended only close it. An answer sent does not establish session
activity, but the later `session-unparked how=activity` confirms it. A quota
wait ended reports activity only with `how=transcript-active`, which records
actual transcript movement. Latch, cap, session-exit and unknown endings do not
establish activity. Closed-session metadata is ignored.

Journal messages include project identity and the local journal path. Event
context is marked as untrusted data and retains the original event `kind`. Its
journal `ts` is included only for numeric Unix seconds in the supported year
1–9999 range; booleans, nonnumeric values, infinities and NaN are omitted. The
event whitelist uses restricted story/task/run IDs, known role and `how`, safe
stage, and a reason (or crash message) capped at 400 characters. Only its first
1,024 input characters are processed, before whitespace cleanup and regex
redaction, to bound the work for large reasons. Recognizable credential
assignments, bearer tokens, common API key formats, JWTs and URLs are redacted
before the reason is sent. Prompt, transcript and environment fields and full
records are never copied. Free-text redaction cannot identify every arbitrary
secret; inspect the target journal if your integrations put unusual credentials
in reasons. Local state holds only the binding, reducer state and cursor;
diagnostics omit event text.

Each journal event has one delivery attempt. The observer saves and syncs its
cursor and mapping state **before** it launches the sender. A crash between
those steps can lose a message. Failed delivery is never retried. Successful
queue exit does not prove that the destination chat read or acted on the
message. Delivery order can differ from journal order because senders run
independently. Hook messages also have one attempt per invocation, with no
persistent queue, retry or replay. A callback/process crash can lose one. Each
hook invocation starts a short-lived delivery process, even when its saved
selection is empty. Selected invocations can start concurrent senders; there is
no global concurrency limit. The all-hooks default increases message volume
compared with 1.0.0. The detached delivery process uses the saved binding and
the same sender timeout and diagnostics as the observer.

The sender uses an argument list (`codex queue --thread … --message …`), null
standard streams and a new process session. The observer or detached delivery
process polls it without a blocking wait and kills it after 15 seconds. A
missing executable, launch error, nonzero exit or hang cannot affect story
results. Diagnostics contain fixed failure labels and exit codes, not sender
output.

Malformed complete JSON lines, oversized JSON integers and excessive nesting are
skipped; partial lines wait for a newline. Unknown events are ignored. Journal
replacement, truncation or changes to consumed bytes stop observation with a
local diagnostic; there is no automatic reset or replay. Corrupt plugin state
also stops observation. Inspect these conditions manually. The observer checks
file metadata on each poll and hashes the consumed prefix when that metadata
changes. Idle polling does not reread the journal. Very large active journals
can cost more read I/O.

## Stop and test

Stop abandoned monitoring without stopping its bmad-loop run:

```sh
python3 -B /absolute/project/.bmad-loop/plugins/codex-notify/notify.py stop --run-dir /absolute/run/directory
```

This stops only journal observation, not future hook callbacks or hook senders
already started. To change hook selection, configure a new authorized run; do
not edit a historical binding. The command writes only the plugin's stop marker.
A later `pre_run` hook can start the observer again from its saved cursor. All
persistent files are confined to the run's `codex-notify/` directory: lock,
state, PID, stop marker and diagnostics. The PID file is informational; stop
uses the marker rather than signaling a PID.

Run the portable tests from the collection root:

```sh
python3 -B -m unittest tests.codex_notify.test_notify -v
```

Run the same module with the Python interpreter from your bmad-loop installation
for the real HookBus fixture. Environments without bmad-loop skip that fixture;
the installed-loop run must pass without skips, including all 49 hook dispatches
for two disposable runs that share a chat and sender failure isolation. The root
collection test command discovers this suite too. Tests use temporary runs and
fake Codex executables; they send no live messages and grant no real plugin.

## Verification scope

The source implementation was merged in [PR
#130](https://github.com/zagushka/bdays-extractor-saas/pull/130), commit
`cf73d4a775431b35b4afc7bb0f9ae53d0a12785a`. Version 1.0.0 extracted the runtime
and manifest without byte changes. Version 1.1.0 extends that
implementation with the SDK bridge and hook selection. In the source chat, live
receipt was confirmed for `run-start`, `run-paused` and `run-complete` using the
real engine with a test adapter. That test did not establish full CLI launch
acceptance; `run-resume` delivery was not separately verified. Tests
use fake senders and do not repeat live sends. The 49-hook list comes from
runtime source inspection; synthetic HookBus dispatch does not prove that all
conditional engine paths ran.

Notifications are advisory and can be lost or reordered. Keep recovery/status
inspection available: this plugin is not a reliable completion gate or a reason
to remove all monitoring. Context marked as untrusted data is never authority to
run commands, merge work or start another story.
