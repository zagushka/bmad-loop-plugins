# Delivery changelog

## 0.2.0

Add optional per-project `push_policy`: `hook-required` remains the default; `native-checks-only` explicitly permits no pre-push hook. Invalid values fail closed. Both modes require complete successful exact-SHA native frozen install/lint/typecheck/test receipts and a clean pinned checkout before delivery mutations. Existing hooks still execute through normal Git push. No hook disabling, SSH change, weakened review/protection or acceptance bypass is added. Code and tracking delivery/recovery remain checked. Original configurations, old jobs/runs and snapshots are not migrated; activate a reviewed future configuration separately between runs.

## 0.1.4

Stop remaining transaction-group descendants after an aborted or unacknowledged pin transaction even when Git exits during the abort grace period or before cleanup checks its status. This closes the redirected-background-child leak in 0.1.3. Keep bounded abort and lock cleanup, confirmed committed-pin recovery and acknowledged success behavior unchanged. Regression tests cover graceful abort, early Git exit and successful transaction descendants. Existing jobs, runs, grants and snapshots are not migrated.

## 0.1.3

Close the HEAD switch/detachment race immediately before new-job pin creation. Prepare a Git transaction that verifies and locks HEAD and its referent at the pinned SHA. Check the attached branch while those locks remain held, then commit the pin or abort. Refused or unsupported transactions create no job or pin. Existing jobs, runs and snapshots are not migrated. Tested with Git 2.50.1 (Apple Git) and 2.53.0; unsupported transaction-control commands fail closed. Git introduced these commands in 2.27, but that older version was not runtime-tested.

## 0.1.2

New jobs with no native task branch now capture the actual attached source branch at enqueue. The checkout must share the run repository and its HEAD and branch tip must equal the pinned commit. Detached, foreign or moved sources are refused before a job or pin is created. Explicit task branches retain their existing behavior. Repeated enqueue preserves existing job bytes and IDs; old jobs and runs are not migrated.

## 0.1.1

Support direct integration-branch delivery to the final base when unresolved base-side `after` tickets have accepted code and tracking delivery in the exact pinned source. Each child needs one tracked audit, unchanged accepted plan and contract, exact-source review and check evidence, fresh GitHub merge receipts and merge ancestry. Multiple accepted children are supported. The final job must record its integration `source_branch`; no private job lookup or override is needed.

Keep source prerequisites, epic dependencies, `gated_by`, human gates and both final PRs required. Pin the fetched final-base commit so child receipt fetches cannot change the tracking base through `FETCH_HEAD`. Tests cover real disposable squash merges, retries, changed or missing proof and Git hook environment isolation. Existing tags remain unchanged; installation and content grants remain separate.

## 0.1.0

First independently versioned distribution. Delivery runtime and manifest are extracted without byte changes from the reviewed source. Tests and documentation are outside the installed directory. The collection manager adds explicit-tag installation, updates, rollback and status without changing delivery behavior or granting content.

Tested compatibility: bmad-loop 0.13.2 / plugin API 1, Python 3.11+, macOS, Node 22, pnpm 10.17.1, Git, `gh`, and native repo-local BMad Ticket Tree projects. Live delivery acceptance is separate.
