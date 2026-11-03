# Status Notify changelog

## 2.0.0 — Unreleased

- Add `debounce_seconds`: a nonnegative integer quiet window, with zero for
  immediate delivery. Each selected message resets a positive window. Journal
  and hook messages share one batch per run, including after observer completion.
- Pass a JSON array of strings through `{message}` for every new run binding,
  including immediate delivery. This changes the sender contract. Existing saved
  bindings retain plain strings and immediate delivery without migration.
- Freeze the window per run. Keep batch state separate from observer state and
  claim batches before each single sender attempt. No retry or receipt guarantee.
- Keep one waiting batch worker per run and split large buffers into successive
  JSON arrays to limit the size of command arguments.

Compatibility: unchanged Python 3.11+, POSIX, bmad-loop 0.13.2 / API 1.
Not released or installed; the reviewed 1.1.0 tag remains unchanged.

## 1.1.0 — 2026-10-31

- Add independent `journal_events` selection: `*` (default), comma-separated
  exact event kinds, or empty for no journal messages. Set both `hooks` and
  `journal_events` empty to disable all notification messages.
- Freeze journal selection per run and preserve all notifications for old
  bindings without the field. Continue journal state tracking for filtered events.
- Reject invalid journal selections before creating a new binding.

Compatibility: unchanged Python 3.11+, POSIX, bmad-loop 0.13.2 / API 1.
Install from the immutable `status-notify-v1.1.0` tag. Existing installations
need a separate update and content review; the 1.0.0 tag remains unchanged.

## 1.0.0 — 2026-10-31

- Add standalone status-notify with the 49 selectable lifecycle hooks and journal
  status notifications from codex-notify 1.1.0.
- Replace Codex-specific sender settings with an explicit sender_command string
  and destination; BMAD_STATUS_NOTIFY_DESTINATION overrides the recipient.
- Parse POSIX command arguments before substituting destination and message,
  without a shell; validate placeholders and freeze the binding per run.
- Retain advisory errors, 15-second sender timeout, privacy filters, single
  attempts, journal integrity checks and independent run state.
- Document the explicit between-runs transition from deprecated codex-notify.
  No live sends, installation, content grants or project migration are implied.

Compatibility: Python 3.11+, POSIX, bmad-loop 0.13.2 / API 1. Sender executables
are external; only controlled senders are tested. Install from the immutable
`status-notify-v1.0.0` tag.
