# Delivery integration

Install a tagged runtime with the collection manager, then review and grant its content with the installed loop command. Installation and content grants are separate. Keep `tickets.mark_done = "never"`. Local `done` queues delivery; it does not establish acceptance.

An authorized orchestrator must verify every criterion, contract, verification instruction, native-plan obligation and review artifact before submitting evidence. The worker checks the evidence identity, completeness and file hashes. It cannot establish the factual quality of an orchestrator's claim. Operator gates, HITL, changed contracts, failed checks, missing GitHub reviews and branch protection stop acceptance.

The worker uses a private clone and exact pinned source commit. It preserves source-project dependencies and Git transport configuration. Code and tracking changes go through separate checked PRs. Acceptance completes only after both merges are confirmed and the private job has `stage: complete`. Retain jobs, receipts and pins as audit/recovery data.

An update does not hot reload workers or change a previous snapshot. Keep existing jobs, evidence, configuration, source pins and snapshots. The [full reference](README.md#update-a-worker-without-changing-an-existing-story-run) gives the bmad-loop 0.13.2 snapshot materialization procedure. Save a private audit copy of `job.json` before retry; retry clears the current error and attempt count. Wait for the old worker lock to be released. Do not delete that lock or create a replacement job. A worker launched while the lock is held exits and does not schedule a takeover.

The distribution manager never runs these operations. Installing or rolling back a release does not authorize a migration, provider change, deployment, live payment, store publication or real PR acceptance. Controlled tests and native checks do not prove live delivery acceptance.
