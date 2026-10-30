# Delivery changelog

## 0.1.1

Support direct integration-branch delivery to the final base when unresolved base-side `after` tickets have accepted code and tracking delivery in the exact pinned source. Each child needs one tracked audit, unchanged accepted plan and contract, exact-source review and check evidence, fresh GitHub merge receipts and merge ancestry. Multiple accepted children are supported. The final job must record its integration `source_branch`; no private job lookup or override is needed.

Keep source prerequisites, epic dependencies, `gated_by`, human gates and both final PRs required. Pin the fetched final-base commit so child receipt fetches cannot change the tracking base through `FETCH_HEAD`. Tests cover real disposable squash merges, retries, changed or missing proof and Git hook environment isolation. Existing tags remain unchanged; installation and content grants remain separate.

## 0.1.0

First independently versioned distribution. Delivery runtime and manifest are extracted without byte changes from the reviewed source. Tests and documentation are outside the installed directory. The collection manager adds explicit-tag installation, updates, rollback and status without changing delivery behavior or granting content.

Tested compatibility: bmad-loop 0.13.2 / plugin API 1, Python 3.11+, macOS, Node 22, pnpm 10.17.1, Git, `gh`, and native repo-local BMad Ticket Tree projects. Live delivery acceptance is separate.
