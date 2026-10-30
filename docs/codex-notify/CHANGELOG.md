# Codex Notify changelog

## 1.0.0

First independent collection release of the reviewed plugin from PR #130.
Runtime and manifest bytes are unchanged. Documentation/tests are outside the
payload, with a runtime MIT notice and compatibility metadata for bmad-loop
0.13.2 / API 1. Use Python 3.11+, POSIX (macOS or Linux), and Codex CLI with
`codex queue`. The collection manager handles versioned install/update/status;
configuration, grants and live observers remain separate.
