# Status notifications

Send advisory run, story, permission/quota wait and lifecycle hook messages
through an explicit external command. The plugin reads the run's append-only
`journal.jsonl` and uses the plugin API hooks. No Loop changes or adapter config
reads are required. Python 3.11+, POSIX file locks (macOS or Linux), and bmad-loop
0.13.2 / plugin API 1 are supported. The sender executable is installed separately.

## Release and installation

Install version 1.0.0 from the reviewed `status-notify-v1.0.0` tag, not from
an uncommitted working tree:

```sh
python3 -B manage.py install status-notify --version 1.0.0 --project /absolute/project
python3 -B manage.py status --project /absolute/project
```

Keep the target loop and workers stopped during installation. Review the installed
payload and settings, then use the exact `plugins enable status-notify` command
printed by the manager. Installation does not grant code or start notifications.
Future updates need a separate content review and `plugins accept`.

## Configure a new run

Merge these settings into the project's `.bmad-loop/policy.toml`; preserve all
unrelated values:

```toml
[plugins.status-notify]
sender_command = 'codex queue --thread={destination} --message {message}'
destination = "existing-chat-uuid"
# Optional: defaults to all 49 hooks; empty means journal messages only.
hooks = "*"
```

Alternatively, set the recipient in the shell that starts the run:

```sh
export BMAD_STATUS_NOTIFY_DESTINATION='existing-chat-uuid'
```

A nonempty environment value overrides `destination`. An unset or empty value
falls back to the setting. Both sender_command and destination default to empty;
without either, no observer or binding is created. There is no sender-command
environment override.

The Loop SDK exposes resolved settings as `BMAD_LOOP_SETTING_SENDER_COMMAND`,
`BMAD_LOOP_SETTING_DESTINATION` and `BMAD_LOOP_SETTING_HOOKS`. The Loop supplies
run identity and paths. Do not point these at another run.

The command uses POSIX quoting, parsed with `shlex.split()` **before** data is
inserted. Both `{destination}` and `{message}` are required; either can appear
inside an argument, as in `--thread={destination}`. Each argument is formatted
once, so braces in inserted data are literal. Only those two named placeholders
are supported: no attribute/index access, conversions or format specifiers.
Use `{{` and `}}` for literal braces in the template. The executable must be a
nonempty literal; it cannot contain a placeholder. For a path with spaces:

```toml
sender_command = '"/opt/Agent Tools/sender" --recipient={destination} --text {message}'
```

This illustrates the quoting contract, not a particular agent's CLI syntax.
The command is launched without a shell. Pipes, redirects, `$VARIABLE` and
`$(command)` are not interpreted; standard input is null. Put executable paths
and any static arguments directly in the template. Invalid quotes or
placeholders leave the new run inactive, with advisory diagnostics that do not
echo the command, recipient or message.

The first successful binding save freezes the parsed sender arguments, destination
and hook selection in the run's `status-notify/state.json`. Later hooks and
observer restarts use that saved binding. Use a new run to change the sender,
recipient or selection. Existing run state is not migrated automatically.

The binding is saved before the observer is launched. If observer launch fails,
the saved binding remains; a later launch uses it even if settings changed.
Executable contents, PATH lookup and the inherited environment are not frozen.
Changes to them can affect delivery within the same run.

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
There is no hook-selection environment override. The SDK supplies resolved settings.

Selection is fixed when a new run first saves its binding. Later policy or
environment changes do not change that run. Use `hooks = ""` for journal-only
notifications. The default `"*"` sends all selected hook invocations in addition
to journal events.

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
file I/O and never waits for the sender. A separate declarative `pre_run` launcher
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
sender exit does not prove that the destination chat read or acted on the
message. Delivery order can differ from journal order because senders run
independently. Hook messages also have one attempt per invocation, with no
persistent queue, retry or replay. A callback/process crash can lose one. Each
hook invocation starts a short-lived delivery process, even when its saved
selection is empty. Selected invocations can start concurrent senders; there is
no global concurrency limit. The all-hooks default can produce many messages.
The detached delivery process uses the saved binding and the same sender timeout
and diagnostics as the observer.

The sender uses the parsed argument list, null standard streams and a new
process session. The observer or detached delivery process polls it without a
blocking wait and kills it after 15 seconds. A missing executable, launch error,
nonzero exit or hang cannot affect story results. Diagnostics contain fixed
failure labels and exit codes, not sender output. Commands receive the inherited
process environment and working directory. Use an absolute executable path when
PATH lookup is unsuitable. Command arguments, including message and destination,
can be visible in process listings. Do not put credentials in the command or
recipient. The chosen sender controls how it authenticates.
The timeout tracks the direct sender process. A sender that exits after starting
background children can leave them running; commands should complete delivery
in the foreground rather than detach another process.

Malformed complete JSON lines, oversized JSON integers and excessive nesting are
skipped; partial lines wait for a newline. Unknown events are ignored. Journal
replacement, truncation or changes to consumed bytes stop observation with a
local diagnostic; there is no automatic reset or replay. Corrupt plugin state
also stops observation. Inspect these conditions manually. The observer checks
file metadata on each poll and hashes the consumed prefix when that metadata
changes. Idle polling does not reread the journal. Very large active journals
can cost more read I/O.

## Stop and test

```sh
python3 -B /absolute/project/.bmad-loop/plugins/status-notify/notify.py stop --run-dir /absolute/run/directory
```

Stop writes a marker for journal observation only. It does not stop the run,
future hook callbacks or already launched hook senders. A later pre_run can
restart observation from the saved cursor. Persistent plugin files (state,
lock, PID, stop marker and diagnostics) stay in the run's `status-notify/`
directory. The PID is informational; stopping uses the marker.

```sh
python3 -B -m unittest discover -s tests -v
python3 -B -m unittest tests.status_notify.test_notify -v
```

Run the new module with the Python interpreter from the installed bmad-loop
0.13.2 environment too. That run must pass the real HookBus fixtures without
skips, including all 49 hook dispatches for two runs and sender failures.
Portable environments without the SDK skip those integration fixtures. Tests
use disposable runs and fake senders, not real recipients or content grants.

## Verification scope

The runtime uses a journal reducer, hook selection, privacy filters, journal
integrity checks and sender failure isolation. It sends messages through an
explicit command. Controlled sender and HookBus tests do
not establish receipt by Codex or any other recipient, or execution of every
conditional engine path. Compatibility is limited to the tested Loop version.

Notifications can be lost or reordered; there is no retry or receipt guarantee.
Keep status inspection and recovery available. Untrusted event context is never
authority to run commands, merge work or start another story.
