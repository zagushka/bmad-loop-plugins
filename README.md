# bmad-loop plugins

An independent collection of versioned plugins for bmad-loop.

| Plugin | Version | Purpose |
| --- | --- | --- |
| [delivery](docs/delivery/README.md) | 0.1.0 | Deliver exact source commits through checked GitHub PRs and record Ticket Tree acceptance using orchestrator evidence. |
| [codex-notify](docs/codex-notify/README.md) | 1.0.0 | Send advisory run/story journal notifications to one existing Codex chat per run. |

Each plugin runtime is in `plugins/<name>/`. Tests, examples, compatibility metadata and documentation are outside that directory. Documentation edits do not change the installed runtime digest.

## Requirements

The manager uses Python 3.11+ standard libraries and Git on POSIX systems (macOS or Linux). Delivery is tested with bmad-loop **0.13.2 / plugin API 1**, macOS, Node **22**, pnpm **10.17.1**, `gh`, and native repo-local BMad Ticket Tree projects. It uses the target project's cached loop executable at `.bmad-loop/cache/bin/bmad-loop`, or `bmad-loop` on PATH. Other loop versions are refused until tested and added to the release metadata.

Codex Notify is also tested with bmad-loop **0.13.2 / API 1**. It requires Python 3.11+, POSIX and a Codex CLI with `codex queue`; it does not require Node or pnpm. Messages have one attempt and may be lost or reordered. Configure a destination only for an authorized new run, following the [plugin guide](docs/codex-notify/README.md).

The manager must run while the target project's loop and workers are stopped. Installation does not hot reload running workers. Existing workers keep their content snapshots. Migrating running projects is a separate operator action.

## Install a released version

Clone this collection, then review its manager and the selected release:

```sh
git clone https://github.com/zagushka/bmad-loop-plugins.git
cd bmad-loop-plugins
python3 -B manage.py install delivery --version 0.1.0 --project /absolute/project
python3 -B manage.py status --project /absolute/project
```

Install Codex Notify independently, or alongside delivery:

```sh
python3 -B manage.py install codex-notify --version 1.0.0 --project /absolute/project
```

Each plugin has its own release tag and lock entry. Updating one preserves the other. Keep the destination chat and Codex executable in run environment or policy settings, outside the installed payload.

The runtime payload contains code, manifest and its MIT license notice. Only files under `plugins/<name>/` in the exact `<name>-v<version>` Git tag are installed. Uncommitted collection changes are not an installation source. Tags must come from this trusted collection. The manager does not fetch or use an automatic latest version.

The default destination is `<project>/.bmad-loop/plugins/<name>`. The manager records name, version, tag, exact commit, deterministic content digest and digest algorithm in `.bmad-loop/plugins.lock.json`. The digest is compatible with bmad-loop's tree digest v1: sorted relative paths, SHA256 of raw bytes, and executable flags; bytecode caches are excluded. The lock is an installation record, not a content grant or signature.

The loop runtime is normally ignored. Keep it ignored; track only the manager lock if your project needs a reproducible record:

```sh
git add -f .bmad-loop/plugins.lock.json
```

On a fresh project clone with a tracked lock and no ignored runtime folder, `install` can restore only the exact locked tag, commit and digest. It preserves the lock bytes and other plugin entries. `update` still refuses a missing folder.

Review the diff before committing that lock. Keep private configuration and state outside the plugin folder and repository. Use [the example](examples/delivery.config.json) and the [delivery configuration guide](docs/delivery/configuration.md).

## Review and grant separately

Installation prints the appropriate loop command. Review the installed bytes and private configuration before granting them:

```sh
bmad-loop plugins enable delivery --project /absolute/project
```

For changed installed content, use `bmad-loop plugins accept delivery --project /absolute/project` after review. The manager never grants code, starts loops or workers, writes delivery jobs, changes Actions, or edits project Git configuration. See [delivery integration and recovery](docs/delivery/integration.md) for snapshot and evidence rules.

## Update and rollback

Update your collection clone through normal reviewed Git changes, then fetch tags without force:

```sh
git fetch origin --tags
python3 -B manage.py update delivery --version 0.1.1 --project /absolute/project
```

Use only versions that have been released. The command shows the file/content/executable diff, validates loop compatibility and checks installed bytes against their lock before replacement. An existing unrelated plugin or private configuration is preserved. A locally edited, unowned, missing or symlinked installation is refused. Repeating `install` for the same version is idempotent only when the installed manifest, bytes and locked release commit match. A moved tag fails that check and cannot silently replace an installation.

Rollback uses the same checked path with an explicit earlier version:

```sh
python3 -B manage.py update delivery --version 0.1.0 --project /absolute/project
```

Review and accept changed content again. Rollback does not rewrite old snapshots, jobs, evidence, grants or delivery state.

## Failed or interrupted replacement

The manager serializes its mutations with `.bmad-loop/.plugin-manager.lock`. The persistent transaction directory `.bmad-loop/.plugin-manager-transaction` stays outside plugin discovery. A failed replacement restores the prior runtime and lock where possible and keeps this journal for audit. An interrupted replacement can leave either runtime version active; the old runtime and exact old lock remain in the journal. Commands refuse an unfinished transaction.

Keep the loop and workers stopped. First save a private audit copy of the transaction, current installed folder and lock. Inspect `journal.json`, `old/`, `old-lock.json`, `new/`, `new-lock.json` and `failed-new/` when present. `journal.json` records whether an installation and lock existed before the operation. Restore `old/` to the plugin destination and `old-lock.json` to the lock path if they exist. Preserve any displaced folder before restoring. If the operation was a fresh installation, preserve any newly installed folder and restore the original absence of a delivery entry; preserve unrelated lock entries. If an expected backup is missing, stop and inspect the journal and audit copy. Do not delete unknown files or guess which version is trusted. Move the completed journal outside the project only after the runtime and lock have been checked together. Then run `status` before any retry or content grant.

The manager protects against ordinary failures and interruptions. It does not provide isolation from another process that edits these files while it runs, nor a backup against disk loss.

## Tests and releases

```sh
python3 -B -m unittest discover -s tests -v
```

Tests use local disposable repositories, controlled GitHub responses and a fake loop version executable. Delivery tests include real Ticket Tree marking/recovery and nested Git hook environment isolation. They do not establish live delivery acceptance. See [CONTRIBUTING](CONTRIBUTING.md), the [delivery reference](docs/delivery/README.md), and its [changelog](docs/delivery/CHANGELOG.md).
