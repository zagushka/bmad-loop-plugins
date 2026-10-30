# Repository guide

This is an independent collection of bmad-loop plugins. Each runtime lives in `plugins/<name>/`. Documentation, tests and examples stay outside runtime directories because bmad-loop grants hash the complete installed directory.

- Keep Python 3.11+ standard-library code; no runtime dependency is needed for the manager.
- Source plugin versions from explicit `<name>-v<version>` Git tags. Never update an installed plugin from an uncommitted working tree or automatically grant changed code.
- Preserve local plugin edits, private configuration, jobs, pins, evidence, grants and existing snapshots. An install/update must not start a loop or worker.
- `delivery` is initially tested with bmad-loop 0.13.2 / API 1, macOS, Node 22, pnpm 10.17.1 and native repo-local BMad Ticket Tree projects. Do not broaden delivery behavior during extraction.
- Keep secrets, real job/state data, personal paths and original project history out of this public repository.
- Run `python3 -B -m unittest discover -s tests -v`. Preserve the nested test that checks Git hook environment isolation. Runtime changes need independent review before release.
- Keep release tags immutable. Each plugin has its own version, changelog and compatibility statement. Publish only reviewed commits; a plugin grant remains local and separate from installation.
