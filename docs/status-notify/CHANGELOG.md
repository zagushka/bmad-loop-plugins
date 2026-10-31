# Status Notify changelog

## 1.0.0 — unreleased

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
are external; only controlled senders are tested. Use the immutable tag
`status-notify-v1.0.0` after publication; source preparation is not publication.
