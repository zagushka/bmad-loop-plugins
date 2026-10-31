# Codex Notify changelog

## 1.1.0 (unreleased)

- Add an API-1 bridge for all 49 lifecycle hooks emitted by bmad-loop 0.13.2,
  including role-specific sessions, commit, integration, rollback and sweep.
- Add `hooks`: `"*"` (new-run default), comma-separated exact names, or empty for
  journal-only messages. Freeze selection with the per-run destination; old
  bindings without this field stay journal-only. Invalid selection is advisory
  and does not initialize notifications for a new run.
- Send hook invocation messages through detached processes with the existing
  bounded sender. Keep exact dispatch/context fields, privacy limits, journal
  pauses such as spec-approval, and failure isolation. No Loop changes, live
  notifications, activation, trust changes or publication are part of this change.

## 1.0.0

First independent collection release of the reviewed plugin from PR #130.
Runtime and manifest bytes are unchanged. Documentation/tests are outside the
payload, with a runtime MIT notice and compatibility metadata for bmad-loop
0.13.2 / API 1. Use Python 3.11+, POSIX (macOS or Linux), and Codex CLI with
`codex queue`. The collection manager handles versioned install/update/status;
configuration, grants and live observers remain separate.
