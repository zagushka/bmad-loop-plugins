# Contributing

Use Python 3.11+ standard libraries. Keep runtime files in `plugins/<name>/`; tests, documentation and examples belong outside the runtime digest boundary. Do not add private configuration, jobs, evidence, credentials, project history or personal paths.

Run `python3 -B -m unittest discover -s tests -v`. Use disposable projects for manager checks. Keep all delivery tests and their nested Git hook environment test. Tests must not mutate GitHub, enable workflows, start real delivery or access providers.

Delivery initially supports only bmad-loop 0.13.2 / API 1. A compatibility change needs tests on the actual loop version. Runtime changes require independent review. Update `plugin.toml`, compatibility metadata and `docs/<name>/CHANGELOG.md` for that plugin's release. Do not infer live delivery acceptance from controlled tests.

Publish only reviewed main commits. Tag each plugin independently as `<name>-v<X.Y.Z>`, for example `delivery-v0.1.0`. Keep tags immutable. Before publication, verify runtime hashes, run the suite and review the exact commit. After tagging, test install, status, repeat install, update and rollback in a disposable supported project. Do not execute the plugin to test installation. Verify public repository visibility and that the published tag resolves to the reviewed main commit.

Do not add an automatic latest-version path or automatic content grants. Keep the manager clone updated through normal reviewed Git changes and fetch tags without force. A distribution change does not authorize running-project migration or delivery acceptance.
