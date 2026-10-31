# Status Notify changelog

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
