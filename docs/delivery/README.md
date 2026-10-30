# Independent ticket delivery

This project plugin supports bmad-loop 0.13.2 / plugin API 1. It uses Python 3.11+ standard libraries, Git, `gh`, Node 22 and pnpm 10.17.1 on macOS. It does not change the loop scheduler, stop handling, dependency rules or checkpoints. Keep `tickets.mark_done = "never"`.

## Install and grant

Use the collection manager to install a released version into `.bmad-loop/plugins/delivery/` in a supported Ticket Tree repository. Read and review its executable files first. Do not grant changed files until their review is complete.

Create a private JSON configuration **outside the repository**, owned by your user and not writable by other users. Use absolute paths:

```json
{
  "repository": "OWNER/REPOSITORY",
  "base": "main",
  "state_dir": "/absolute/private/delivery-state",
  "orchestrators": ["authorized-delivery-orchestrator"],
  "expected_checks": ["documented-ci-job-name"],
  "command_timeout": 120,
  "check_timeout": 1800,
  "max_attempts": 20,
  "poll_interval": 5,
  "worker_lifetime": 3600
}
```

Set the actual expected check names when Actions are enabled. An explicit empty list is permitted for a repository with Actions disabled. Protected branch checks remain required even when Actions are disabled. An unreadable Actions or protection response stops delivery. The plugin never changes Actions settings.

In the ignored loop policy:

```toml
[tickets]
mark_done = "never"

[plugins.delivery]
config = "/absolute/private/delivery-config.json"
```

After review, grant the plugin through the installed loop command:

```sh
bmad-loop plugins enable delivery --project /absolute/repository
```

A later executable change requires `bmad-loop plugins accept delivery` after a new review. No grant is part of installing this folder. An empty `config` setting keeps the plugin inert.

Origins can use HTTPS, SCP-style SSH or `ssh://`. For SSH, the worker reads the effective hostname through `ssh -G`, including local host aliases. The effective origin host must match `GH_HOST` (default `github.com`), and its owner/repository must match the configured repository. Git keeps the original transport URL and SSH identity; `gh repo view` uses the verified canonical HTTPS URL. Local paths and other transports are not accepted for delivery.

Custom `GIT_SSH_COMMAND`, `GIT_SSH`, `core.sshCommand` or non-OpenSSH `GIT_SSH_VARIANT` overrides stop SSH delivery. The worker cannot verify those transports through plain `ssh -G`. It does not remove or change the overrides.

Hooks run from the loop's verified content snapshot. They capture the commit and reject parked/HITL/checkpoint work, pin the object under `refs/delivery/`, write a durable job and start a detached worker. Hooks do not wait for GitHub, installation or native checks. Hook errors are recorded in `ctx.shared.delivery_error`. Run-boundary reconciliation also writes `reconcile-errors.json`. The worker or first evidence submission creates a private clone. The source checkout and its installed dependencies stay separate.

## Evidence contract

Local `done` is an enqueue signal, not acceptance. An authorized orchestrator must inspect every criterion, source and review artifact before submitting evidence. The private configuration and state directory are the local trust boundary: an `orchestrator` string is an explicit attestation, not a cryptographic signature. Give access only to the authorized delivery process. Do not put secrets in evidence or check output.

Run `status` to obtain the exact identity fields, then supply JSON with this shape:

```json
{
  "repository": "OWNER/REPOSITORY",
  "run": "/absolute/original/run-directory",
  "ticket": "1.1",
  "sha": "40-character-source-commit",
  "base": "main",
  "container": "_bmad-output/initiative-example",
  "orchestrator": "authorized-delivery-orchestrator",
  "operator_gates": [],
  "hitl": false,
  "review": {
    "status": "completed",
    "sha": "40-character-source-commit",
    "artifact": "docs/review.md"
  },
  "contract": {"path": "path/to/bound/story.md"},
  "artifacts": [
    {"path": "docs/review.md", "sha256": "SHA256-of-exact-UTF8-file-bytes"},
    {"path": "path/to/bound/story.md", "sha256": "SHA256-of-exact-UTF8-file-bytes"}
  ],
  "criteria": [
    {
      "criterion": "Exact complete numbered criterion text, including continuation lines.",
      "status": "passed",
      "artifact": "docs/review.md"
    }
  ]
}
```

Every artifact must be tracked at the pinned source commit. Criteria are read from the bound story or entry description. `Acceptance Criteria`, `Local Acceptance Criteria`, `Requirements`, and native-plan `Tasks & Acceptance` sections are recognized without case restrictions. An ordinary entry description needs no added heading. Include every numbered or bullet item and each prose paragraph, with continuation lines, as an exact criterion string; omit list numbers and checkbox prefixes. Bold labels such as `**Acceptance Criteria:**` are labels, not criteria. The entry’s `verify` instructions and native-plan acceptance/tasks also need evidence. Identical obligations require one record. For entry-only tickets, use that entry’s `tickets/tickets.toml` as the contract artifact. Include the native plan as a hashed artifact too. An unrelated document cannot supply acceptance criteria. Every obligation must have one passed evidence record. Missing verification or plan obligations stay pending. The orchestrator is responsible for factual evidence quality; this worker verifies identity, completeness and artifact integrity.

The worker rechecks the current base ticket and gates before merge and before marking. A changed contract, verification instruction, plan obligation, HITL flag or blocked/parked state stops acceptance. Only frontmatter supplies plan/story gates: empty operator-action lists or blocks are clear; nonempty or unrecognized values stop acceptance. Historical body text does not change those gates. It executes a frozen install and lint/typecheck/test in its own clone, preserves the configured Git identity and hooks, and makes a normal push with the pre-push hook. Native success is tied to the exact SHA. Actions workflow runs, check runs and statuses are paginated; queued workflows and missing expected checks prevent merge. GitHub reviews and merge protection remain enforced. Merges use `--match-head-commit`; a queue request is not a confirmed merge.

### Accepted integration prerequisites

Version 0.1.1 supports a direct integration branch to final-base handoff. The final job must contain a nonempty `source_branch` that differs from its `base`. A child delivery must have used that exact integration branch as its base. Intermediate branch graphs are not supported. Existing jobs with a missing source branch stay pending when this proof is needed; do not edit their identity or evidence to force acceptance.

From version 0.1.2, a new job with an empty or missing native task branch (including `scm.isolation = "none"`) captures the actual attached branch. The source checkout must share the run repository, and both HEAD and the branch tip must equal the pinned source SHA. `target_branch` is not a source branch. Detached HEAD, another repository or a moved source prevents enqueue; no job or delivery pin is created. Existing jobs are returned unchanged, including legacy jobs with an empty source branch. Reconcile cannot reconstruct a missing branch after the checkout has advanced. Explicit native task branches retain their existing behavior.

Only an unresolved base-side `after` ticket row can use this proof. Each dependency must identify exactly one native ticket and one tracked `delivery-JOB_ID.json` audit in the pinned source. The worker recomputes the child job ID and checks repository, container, ticket, plan and branch bindings before it fetches child objects. The child must be `done` with clear gates. Its tracked audit must contain complete exact-source review, artifact, criterion and native check evidence. A saved operational status such as `waiting` after a transient retry does not replace or invalidate that acceptance evidence.

Fresh GitHub reads must confirm the exact child code PR and exactly one tracking PR, their repository, heads, integration base, stable delivery branches and actual merge commits. Both merge commits must be in the pinned final source, with code before tracking. Squash merge receipts are supported; the original PR heads need not be source ancestors. The tracking merge must introduce the bound plan and audit acceptance. The accepted plan and audit must remain unchanged in the pinned source. The child semantic contract must agree across its audited source, accepted tracking merge, current source and current final base. Later changes to other entries in a shared `tickets.toml` do not invalidate that contract; story-file contracts require exact content consistency.

Missing, duplicate, foreign, incomplete or changed proof stays pending before a final push, mark or merge. Source prerequisite rules, epic gates, `gated_by`, operator/HITL gates, current final contracts and exact-head review/check requirements remain in force. Multiple accepted `after` children can be verified independently. The final delivery still needs both its normal code and tracking PR merges. Child proof fetches cannot replace the explicitly pinned final-base SHA used for tracking. No external private child job, special configuration or bypass setting is required.

## Commands and recovery

Hooks start the worker from the **reviewed snapshot**. For manual execution, set `SNAPSHOT` to that worker's content-addressed directory under `<state root>/<project-tag>/_plugin-snapshots/<digest>/`. A run materializes the snapshot; granting alone only records approval of the files. The following uses `SNAPSHOT` to refer to that directory:

```sh
python3 -B "$SNAPSHOT/delivery.py" --config /absolute/config.json status
python3 -B "$SNAPSHOT/delivery.py" --config /absolute/config.json evidence JOB_ID /absolute/evidence.json
python3 -B "$SNAPSHOT/delivery.py" --config /absolute/config.json worker
python3 -B "$SNAPSHOT/delivery.py" --config /absolute/config.json retry JOB_ID
python3 -B "$SNAPSHOT/delivery.py" --config /absolute/config.json reconcile /absolute/repository /absolute/run
```

`evidence` and `retry` queue the job; start `worker` explicitly afterwards. One process lock prevents duplicate workers. Per-job locks protect concurrent status/evidence/retry operations. Each pass advances all runnable jobs using up to four independent clones, so a long check does not hold other jobs behind it. Jobs added during a pass are read on the next pass. The worker polls until no runnable jobs remain or its bounded lifetime ends. Delayed checks use bounded backoff; after `max_attempts`, status says `retry-required`. Resume with `retry` and `worker`. No permanent daemon or periodic automation is installed.

### Update a worker without changing an existing story run

Keep the existing job directory, evidence, source pin, configuration and old snapshot. Do not edit the stored origin or reconcile historical runs. In a separate clean checkout containing the reviewed merged plugin, use the installed bmad-loop 0.13.2 command to accept the new bytes:

```sh
bmad-loop plugins accept delivery --project /absolute/reviewed-checkout
```

Granting alone does not create a snapshot. With the Python interpreter from that same loop installation, this registry API verifies and materializes granted content without importing plugin code or launching/resuming a story:

```python
from pathlib import Path
from bmad_loop.plugins.registry import preflight
from bmad_loop.plugins.store import load_grants
from bmad_loop.plugins.grants import TrustState, unclaimed

project = Path('/absolute/reviewed-checkout').resolve()
grants = load_grants(project)
assert grants.fault is None, grants.fault
verdicts = preflight(project, None, grants, materialize=True)
assert not any(v.fatal for _, v, _ in verdicts)
assert not unclaimed(grants, [v for _, v, _ in verdicts])
assert any(m.name == 'delivery' and v.state is TrustState.GRANTED for m, v, _ in verdicts)
record = next(r for r in grants.records if r.name == 'delivery')
snapshot = grants.snapshot_root / record.digest
assert (snapshot / 'delivery.py').is_file()
print(snapshot)
```

Use that printed path as `SNAPSHOT`. Wait for the old bounded worker to release `worker.lock`; do not delete the lock file. Save a private copy of the existing `job.json` first: `retry` resets its current error and attempt count. Then run `retry JOB_ID` and `worker` from the new snapshot with the **existing** configuration. Do not replace evidence or create a second job. The worker revalidates existing evidence and reconciles delivery receipts. A worker started while the lock is held exits; it does not schedule a later takeover. Future loop launches in another checkout require their own reviewed installation and content grant.

The run journal closes the post-commit/state-save crash window. Jobs and operation receipts use atomic writes. A stable repository/run/ticket/SHA/base identity gives each job one code branch and one tracking branch. PR queries include closed and merged PRs. On retry the worker reconciles the existing PR and confirmed merge instead of creating another PR. A changed PR head stays pending and requires a new source job and evidence. Source branches can advance or be deleted after enqueue: delivery fetches and validates the durable `refs/delivery/JOB_ID` pin, rather than substituting a live branch. Every confirmed merge must match the repository, destination branch, source head and a valid merge commit.

Only after confirmed code merge does the worker run supported `tickets.py find` and `mark ... done`. It materializes a minimal ignored `_bmad/custom` directory for explicit-folder repo-store commands. The generated plan/audit commit goes through its own checked tracking PR. The tracked `delivery-JOB_ID.json` records code acceptance evidence and the stable tracking branch. The private final `job.json` is the complete delivery audit: both PRs, both merge commits, exact local check receipts, Actions state and source evidence. Tracking acceptance is complete only when `stage` is `complete`. Retain this state directory and pinned refs as audit/recovery data; no automatic cleanup deletes them.

## Verification

```sh
python3 -B -m unittest discover -s tests -v
```

The collection test command includes this suite. Tests use disposable Git repositories and controlled GitHub responses. Full advancement uses real local pushes, commits and the supported ticket mark command. Tests interrupt after push, code PR creation, code merge, mark, tracking commit, tracking PR creation and tracking merge; retry reconciles one PR per stage. They also cover waiting jobs beside runnable delivery, worker polling and lock handoff. A nested suite runs under Git hook environment variables and verifies that the parent repository configuration, identity, HEAD, index and files stay unchanged. They do not enable workflows, merge a real PR, deploy, change a provider or access the browser. Native checks and GitHub acceptance for a real delivered story are separate evidence.
