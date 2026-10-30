"""Disposable Git and controlled GitHub transport tests; no network or providers."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]

spec = importlib.util.spec_from_file_location("delivery", ROOT / "plugins/delivery/delivery.py")
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        d.git(self.repo, "init", "-b", "main")
        d.git(self.repo, "config", "user.name", "Disposable Test")
        d.git(self.repo, "config", "user.email", "test@example.invalid")
        hook = self.repo / ".git/hooks/pre-push"
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o700)
        (self.repo / "source").write_text("initial")
        d.git(self.repo, "add", ".")
        d.git(self.repo, "commit", "-m", "initial")
        self.sha = d.git(self.repo, "rev-parse", "HEAD")
        self.remote = self.root / "remote.git"
        d.git(self.repo, "clone", "--bare", str(self.repo), str(self.remote))
        d.git(self.repo, "remote", "add", "origin", str(self.remote))
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.cfg = dict(repository="owner/repo", base="main", state_dir=str(self.root / "state"),
                        orchestrators=["authorized"], expected_checks=["ci"])
        self.state = dict(repo_root=str(self.repo), initiative_folder=str(self.repo / "initiative"),
                          tasks={"1.1": dict(phase="done", commit_sha=self.sha, operator_actions=[],
                          ticket=dict(ref="1.1", plan=str(self.repo / "initiative/plan.md"), title="Test"))})
        d.atomic(self.run_dir / "state.json", self.state)

    def queued(self):
        key = d.enqueue(self.cfg, self.repo, self.run_dir, "1.1")
        path = Path(self.cfg["state_dir"]) / key
        return path, d.read(path / "job.json")

    def test_origin_alias_uses_real_ssh_configuration_without_changing_origin(self):
        ssh_config = self.root / "ssh_config"
        ssh_config.write_text("Host delivery-alias\n HostName github.com\n User git\nHost wrong-alias\n HostName evil.example\n")
        original_run = d.run
        calls = []
        def run(args, *a, **kw):
            calls.append(args)
            if args[:2] == ["ssh", "-G"]:
                args = ["ssh", "-F", str(ssh_config), *args[1:]]
            return original_run(args, *a, **kw)
        for origin in ("git@delivery-alias:owner/repo.git", "ssh://git@delivery-alias:22/owner/repo.git"):
            with self.subTest(origin=origin), patch.dict(os.environ, {"GH_HOST": "github.com"}), patch.object(d, "run", side_effect=run):
                self.assertEqual(d.github_origin(origin, "owner/repo"), "https://github.com/owner/repo")
        ssh_calls = [args for args in calls if args[0] == "ssh"]
        self.assertEqual(ssh_calls[0], ["ssh", "-G", "git@delivery-alias"])
        self.assertEqual(ssh_calls[1], ["ssh", "-G", "-p", "22", "git@delivery-alias"])
        with patch.object(d, "run", side_effect=run), self.assertRaisesRegex(d.Pending, "origin host"):
            d.github_origin("git@wrong-alias:owner/repo.git", "owner/repo")
        with patch.dict(os.environ, {"GH_HOST": "github.com"}):
            self.assertEqual(d.github_origin("https://github.com/owner/repo.git", "owner/repo"),
                             "https://github.com/owner/repo")

    def test_origin_rejects_wrong_identity_and_lookup_failure_before_delivery(self):
        for origin in ("https://evil.example/owner/repo", "git@alias:other/repo.git",
                       "file:///owner/repo", str(self.remote), "ext::command", "ssh://-oProxyCommand=x/owner/repo",
                       "https://user@github.com/owner/repo", "https://github.com/owner/repo?query"):
            with self.subTest(origin=origin), self.assertRaises(d.Pending):
                d.github_origin(origin, "owner/repo")
        for output in ("hostname evil.example", "", "hostname github.com\nhostname evil.example"):
            original_run = d.run
            def run(args, *a, **kw):
                return output if args[0] == "ssh" else original_run(args, *a, **kw)
            with patch.object(d, "run", side_effect=run), self.assertRaises(d.Pending):
                d.github_origin("git@alias:owner/repo", "owner/repo")
        with patch.object(d, "run", side_effect=RuntimeError("ssh failed")), self.assertRaises(RuntimeError):
            d.github_origin("git@alias:owner/repo", "owner/repo")
        directory, job = self.queued()
        worker = d.Worker(self.cfg)
        before = d.read(directory / "job.json")
        with patch.object(worker, "initialize"), patch.object(worker, "evidence"), patch.object(worker, "gh") as gh, patch.object(worker, "deliver") as deliver:
            with self.assertRaises(d.Pending):
                worker.advance(directory, job)
            gh.assert_not_called()
            deliver.assert_not_called()
        self.assertEqual(d.read(directory / "job.json"), before)

    def test_custom_git_ssh_transport_cannot_bypass_host_verification(self):
        for env in ({"GIT_SSH_COMMAND": "ssh -F /other/config"}, {"GIT_SSH": "/custom/ssh"},
                    {"GIT_SSH_VARIANT": "plink"}):
            with patch.dict(os.environ, env), self.assertRaisesRegex(d.Pending, "custom Git SSH"):
                d.github_origin("git@alias:owner/repo.git", "owner/repo", self.repo)
        d.git(self.repo, "config", "core.sshCommand", "ssh -F /other/config")
        with self.assertRaisesRegex(d.Pending, "custom Git SSH"):
            d.github_origin("git@alias:owner/repo.git", "owner/repo", self.repo)

    def test_enqueue_pins_and_deduplicates_without_checkout_changes(self):
        before = d.git(self.repo, "status", "--porcelain")
        directory, job = self.queued()
        self.assertEqual(d.enqueue(self.cfg, self.repo, self.run_dir, "1.1"), job["id"])
        self.assertEqual(d.git(self.repo, "rev-parse", f"refs/delivery/{job['id']}"), self.sha)
        self.assertFalse((directory / "checkout").exists())
        self.assertEqual(d.git(self.repo, "status", "--porcelain"), before)

    def test_original_binding_survives_linked_worktree(self):
        worktree = self.root / "worktree"
        d.git(self.repo, "worktree", "add", "--detach", str(worktree), self.sha)
        directory = d.enqueue(self.cfg, worktree, self.run_dir, "1.1", self.sha, "done")
        self.assertEqual(d.read(Path(self.cfg["state_dir"]) / directory / "job.json")["plan"], "initiative/plan.md")

    def test_parked_and_hitl_do_not_enqueue(self):
        self.assertIsNone(d.enqueue(self.cfg, self.repo, self.run_dir, "1.1", phase="awaiting-operator"))
        self.state["tasks"]["1.1"]["ticket"]["hitl"] = True
        d.atomic(self.run_dir / "state.json", self.state)
        with self.assertRaises(d.Pending):
            self.queued()

    def test_journal_recovers_commit_before_state_save(self):
        self.state["tasks"]["1.1"].update(phase="committing", commit_sha=None)
        d.atomic(self.run_dir / "state.json", self.state)
        (self.run_dir / "journal.jsonl").write_text(json.dumps(dict(kind="story-done", story_key="1.1", commit=self.sha)) + "\n{partial")
        d.reconcile(self.cfg, self.repo, self.run_dir)
        self.assertEqual(len(list(Path(self.cfg["state_dir"]).glob("*/job.json"))), 1)

    def test_one_failed_job_does_not_block_another_and_retry_is_bounded(self):
        directory, job = self.queued()
        second = directory.parent / "second"
        d.atomic(second / "job.json", dict(job, id="second"))
        worker = d.Worker(dict(self.cfg, max_attempts=1))
        seen = []
        def advance(path, entry):
            seen.append(entry["id"])
            if path == directory:
                raise d.Pending("registration delay")
            entry["stage"] = "complete"
            worker.save(path, entry)
        with patch.object(worker, "advance", side_effect=advance):
            worker.pass_once()
        self.assertEqual(len(seen), 2)
        self.assertEqual(d.read(directory / "job.json")["status"], "retry-required")
        self.assertEqual(d.read(second / "job.json")["stage"], "complete")

    def checker(self, enabled=True, registered=True, conclusion="success", protected=False):
        worker = d.Worker(self.cfg)
        def api(endpoint):
            if endpoint.endswith("actions/permissions"):
                return dict(enabled=enabled)
            if endpoint.endswith("/protection"):
                return dict(required_status_checks=dict(contexts=["ci"]))
            if "/branches/" in endpoint:
                return dict(protected=protected)
            if "/actions/runs" in endpoint:
                return dict(workflow_runs=[])
            if "check-runs" in endpoint:
                return dict(check_runs=[dict(name="ci", status="completed", conclusion=conclusion)] if registered else [])
            if "/statuses" in endpoint:
                return []
            raise AssertionError(endpoint)
        worker.api = api
        return worker

    def test_actions_disabled_is_recorded_not_passed(self):
        result = self.checker(False, False).checks(dict(repository="owner/repo"), self.sha, "main")
        self.assertEqual(result["actions"], "disabled")

    def test_actions_registration_delay_unknown_failure_and_protection(self):
        for worker in (self.checker(True, False), self.checker(None), self.checker(True, True, "failure"),
                       self.checker(True, True, "cancelled"), self.checker(False, False, protected=True)):
            with self.subTest(worker=worker), self.assertRaises(d.Pending):
                worker.checks(dict(repository="owner/repo"), self.sha, "main")

    def test_pending_workflow_blocks_even_after_expected_check_passes(self):
        worker = self.checker()
        original = worker.api
        worker.api = lambda endpoint: dict(workflow_runs=[dict(workflow_id=42, status="queued", conclusion=None)]) if "/actions/runs" in endpoint else original(endpoint)
        with self.assertRaisesRegex(d.Pending, "workflow waiting"):
            worker.checks(dict(repository="owner/repo"), self.sha, "main")

    def test_pagination(self):
        worker = d.Worker(self.cfg)
        calls = []
        worker.api = lambda endpoint: calls.append(endpoint) or (list(range(100)) if endpoint.endswith("&page=1") else [100])
        self.assertEqual(len(worker.pages("items")), 101)
        self.assertEqual(len(calls), 2)

    def test_head_change_never_merges(self):
        directory, job = self.queued()
        worker = d.Worker(self.cfg)
        worker.pr = lambda *args: dict(head=dict(sha="0" * 40))
        with patch.object(worker, "gh") as gh, self.assertRaisesRegex(d.Pending, "head changed"):
            worker.deliver(directory, job, self.sha, "branch", "code")
        gh.assert_not_called()

    def test_unverified_external_merge_does_not_accept(self):
        directory, job = self.queued()
        worker = d.Worker(self.cfg)
        worker.pr = lambda *args: dict(head=dict(sha=self.sha), merged_at="now")
        with self.assertRaisesRegex(d.Pending, "saved exact-head"):
            worker.deliver(directory, job, self.sha, "branch", "code")

    def test_saved_merge_reconciles_without_duplicate_mutation(self):
        directory, job = self.queued()
        job["code_verification"] = dict(sha=self.sha)
        worker = d.Worker(self.cfg)
        worker.pr = lambda *args: dict(number=1, head=dict(sha=self.sha), merged_at="now")
        worker.api = lambda *args: dict(merged=True, merge_commit_sha=self.sha, head=dict(sha=self.sha), base=dict(ref="main", repo=dict(full_name="owner/repo")))
        with patch.object(worker, "gh") as gh:
            self.assertTrue(worker.deliver(directory, job, self.sha, "branch", "code")["merged"])
        gh.assert_not_called()

    def test_process_lock_excludes_duplicate_worker(self):
        with d.lock(self.root / "lock") as first:
            with d.lock(self.root / "lock") as second:
                self.assertTrue(first)
                self.assertFalse(second)

    def test_missing_and_stale_evidence_stay_pending(self):
        directory, job = self.queued()
        worker = d.Worker(self.cfg)
        with self.assertRaisesRegex(d.Pending, "missing orchestrator"):
            worker.evidence(directory, job)
        d.atomic(directory / "evidence.json", dict(job, sha="0" * 40))
        with self.assertRaisesRegex(d.Pending, "identity mismatch"):
            worker.evidence(directory, job)

    def ticket_fixture(self, new_plan=False):
        import shutil
        script = ROOT / "tests/fixtures/ticket-tree/tickets.py"
        target = self.repo / ".agents/skills/bmad-agent-po/scripts/tickets.py"
        target.parent.mkdir(parents=True)
        shutil.copyfile(script, target)
        initiative = self.repo / "initiative"
        epic = initiative / "tickets/epic-test"
        (epic / "tickets").mkdir(parents=True)
        (initiative / "initiative.md").write_text("---\ntype: initiative\ntitle: Test\n---\n")
        (initiative / "tickets/tickets.toml").write_text('[[epic]]\nid = 1\nslug = "epic-test"\ntitle = "Test"\n')
        (epic / "epic-test.md").write_text("---\ntype: epic\ntitle: Test\nparent: initiative\n---\n")
        (epic / "tickets/tickets.toml").write_text('[[entry]]\nid = 1\ntype = "story"\ntitle = "Test"\ndescription = "## Acceptance Criteria\\n\\n1. Given a job, delivery completes."\nhitl = false\n')
        plan = epic / "tickets/story-test-plan.md"
        plan_text = "---\ntitle: Test\nticket: 1\nstatus: built\noperator_actions: []\n---\n"
        if not new_plan:
            plan.write_text(plan_text)
        (self.repo / "review.md").write_text("Completed independent review.\n")
        d.git(self.repo, "add", ".")
        d.git(self.repo, "commit", "-m", "ticket fixture")
        self.sha = d.git(self.repo, "rev-parse", "HEAD")
        d.git(self.repo, "push", "origin", "main")
        if new_plan:
            d.git(self.repo, "checkout", "-b", "story")
            plan.write_text(plan_text)
            d.git(self.repo, "add", ".")
            d.git(self.repo, "commit", "-m", "introduce native plan")
            self.sha = d.git(self.repo, "rev-parse", "HEAD")
        self.state["tasks"]["1.1"]["commit_sha"] = self.sha
        self.state["tasks"]["1.1"]["ticket"]["plan"] = str(plan)
        d.atomic(self.run_dir / "state.json", self.state)
        directory, job = self.queued()
        d.git(self.repo, "clone", "--no-hardlinks", str(self.repo), str(directory / "checkout"))
        checkout = directory / "checkout"
        d.git(checkout, "remote", "set-url", "origin", str(self.remote))
        d.git(checkout, "config", "user.name", "Disposable Test")
        d.git(checkout, "config", "user.email", "test@example.invalid")
        d.git(checkout, "config", "core.hooksPath", job["git_settings"]["core.hooksPath"])
        artifacts = [dict(path=p, sha256=hashlib.sha256((self.repo / p).read_bytes()).hexdigest())
                     for p in ("review.md", "initiative/tickets/epic-test/tickets/tickets.toml", str(plan.relative_to(self.repo)))]
        evidence = {k: job[k] for k in ("repository", "run", "ticket", "sha", "base", "container")}
        evidence.update(orchestrator="authorized", operator_gates=[], hitl=False,
                        artifacts=artifacts, review=dict(status="completed", sha=self.sha, artifact="review.md"),
                        contract=dict(path=artifacts[1]["path"]),
                        criteria=[dict(criterion="Given a job, delivery completes.", status="passed", artifact="review.md")])
        d.atomic(directory / "evidence.json", evidence)
        return directory, job, evidence

    def test_real_ticket_script_and_contract_binding(self):
        directory, job, evidence = self.ticket_fixture()
        worker = d.Worker(self.cfg)
        self.assertEqual(worker.evidence(directory, job), evidence)
        evidence["contract"]["path"] = "review.md"
        d.atomic(directory / "evidence.json", evidence)
        with self.assertRaisesRegex(d.Pending, "bound ticket source"):
            worker.evidence(directory, job)

    def test_current_ticket_gate_added_after_enqueue(self):
        directory, job, evidence = self.ticket_fixture()
        path = self.repo / "initiative/tickets/epic-test/tickets/tickets.toml"
        path.write_text(path.read_text().replace("hitl = false", "hitl = true"))
        d.git(self.repo, "add", ".")
        d.git(self.repo, "commit", "-m", "human gate")
        d.git(self.repo, "push", "origin", "main")
        with self.assertRaisesRegex(d.Pending, "gate"):
            d.Worker(self.cfg).current_ticket(directory, job, job["sha"])

    def test_tracking_mark_is_isolated_and_repeatable(self):
        directory, job, evidence = self.ticket_fixture()
        worker = d.Worker(self.cfg)
        job["code_pr"] = dict(number=1, merge_commit_sha=self.sha)
        sha = worker.tracking(directory, job, evidence)
        self.assertEqual(worker.tracking(directory, job, evidence), sha)
        self.assertIn("status: done", d.git(directory / "checkout", "show", f"{sha}:{job['plan']}"))
        self.assertIn("status: built", (self.repo / job["plan"]).read_text())
        self.assertEqual(d.git(self.repo, "status", "--porcelain"), "")

    def test_frontmatter_gate_semantics(self):
        for actions in ("[]", "[ ]", "[\n  ]", "[ \n # comment\n ]", "[] # clear", "", "\n  []", "\n# no actions"):
            d.check_gates(f"---\nstatus: built\noperator_actions: {actions}\n---\noperator_actions: [historical]\n")
        for actions in ('["approve"]', "\n  - approve", "{}", "[", "true"):
            with self.subTest(actions=actions), self.assertRaises(d.Pending):
                d.check_gates(f"---\noperator_actions: {actions}\n---\n")
        with self.assertRaises(d.Pending):
            d.check_gates("---\noperator_actions: []\noperator_actions: []\n---\n")

    def test_empty_plugin_validation_is_inert(self):
        from types import SimpleNamespace
        plugin = d.DeliveryPlugin(None, {})
        plugin.validate(SimpleNamespace())
        plugin.settings = {"config": "configured"}
        with self.assertRaises(Exception):
            plugin.validate(SimpleNamespace())

    def test_real_formats_require_verify_and_plan_obligations(self):
        directory, job, evidence = self.ticket_fixture()
        source = self.repo / evidence["contract"]["path"]
        source.write_text(source.read_text().replace('## Acceptance Criteria\\n\\n1. Given a job, delivery completes.',
            'Deliver the ordinary entry.').replace('hitl = false', 'verify = "Verify real isolated delivery."\nhitl = false'))
        plan = self.repo / job["plan"]
        plan.write_text(plan.read_text() + "\n## Tasks & Acceptance\n\n- [ ] Execute both PRs.\n\n**Acceptance Criteria:**\n\n- Confirm exact merged heads.\n")
        d.git(self.repo, "add", ".")
        d.git(self.repo, "commit", "-m", "real-format obligations")
        self.sha = d.git(self.repo, "rev-parse", "HEAD")
        self.state["tasks"]["1.1"]["commit_sha"] = self.sha
        d.atomic(self.run_dir / "state.json", self.state)
        directory, job = self.queued()
        worker = d.Worker(self.cfg)
        worker.initialize(directory, job)
        evidence.update({k: job[k] for k in ("repository", "run", "ticket", "sha", "base", "container")})
        evidence["review"]["sha"] = self.sha
        for artifact in evidence["artifacts"]:
            artifact["sha256"] = hashlib.sha256((self.repo / artifact["path"]).read_bytes()).hexdigest()
        required = ["Deliver the ordinary entry.", "Verify real isolated delivery.", "Execute both PRs.",
                    "Confirm exact merged heads."]
        evidence["criteria"] = [dict(criterion=c, status="passed", artifact="review.md") for c in required]
        d.atomic(directory / "evidence.json", evidence)
        self.assertEqual(worker.evidence(directory, job), evidence)
        for omitted in ("Verify real isolated delivery.", "Confirm exact merged heads."):
            evidence["criteria"] = [dict(criterion=c, status="passed", artifact="review.md") for c in required if c != omitted]
            d.atomic(directory / "evidence.json", evidence)
            with self.assertRaisesRegex(d.Pending, "every contract, verify and plan"):
                worker.evidence(directory, job)
        self.assertEqual(d.obligations("## Local Acceptance Criteria\n\n1. Complete local checks.\n"), ["Complete local checks."])
        self.assertEqual(d.obligations("## Acceptance criteria\n\n- Complete checks.\n"), ["Complete checks."])

    def transport(self, worker, job):
        """Controlled gh; mutations still push and merge real disposable Git refs."""
        # Local bare remotes replace GitHub only in this controlled transport.
        identity = patch.object(d, "github_origin", return_value="https://github.com/owner/repo")
        identity.start()
        self.addCleanup(identity.stop)
        prs = {}
        counts = {"code": 0, "tracking": 0}
        def api(endpoint):
            if "/pulls?" in endpoint:
                return [p for p in prs.values() if f":{p['head']['ref']}&" in endpoint]
            if "/pulls/" in endpoint:
                return prs[int(endpoint.rsplit("/", 1)[1].split("?")[0])]
            if endpoint.endswith("actions/permissions"):
                return dict(enabled=False)
            if "/branches/" in endpoint:
                return dict(protected=False)
            if "check-runs" in endpoint:
                return dict(check_runs=[])
            if "/statuses" in endpoint:
                return []
            raise AssertionError(endpoint)
        def gh(*args):
            if args[:2] == ("repo", "view"):
                self.assertEqual(args[2], "https://github.com/owner/repo")
                return json.dumps(dict(nameWithOwner="owner/repo"))
            if args[:2] == ("pr", "create"):
                branch = args[args.index("--head") + 1]
                sha = d.git(self.repo, "--git-dir", str(self.remote), "rev-parse", f"refs/heads/{branch}")
                number = len(prs) + 1
                counts[branch.rsplit("-", 1)[1]] += 1
                prs[number] = dict(number=number, state="open", merged=False, merged_at=None,
                    head=dict(sha=sha, ref=branch, repo=dict(full_name="owner/repo")),
                    base=dict(ref="main", repo=dict(full_name="owner/repo")))
                return str(number)
            pr = prs[int(args[2])]
            if args[:2] == ("pr", "view"):
                return json.dumps(dict(headRefOid=pr["head"]["sha"], baseRefName="main",
                    mergeStateStatus="CLEAN", reviewDecision="APPROVED", isDraft=False))
            if args[:2] == ("pr", "merge"):
                self.assertEqual(args[args.index("--match-head-commit") + 1], pr["head"]["sha"])
                d.git(self.repo, "--git-dir", str(self.remote), "update-ref", "refs/heads/main", pr["head"]["sha"])
                pr.update(state="closed", merged=True, merged_at="now", merge_commit_sha=pr["head"]["sha"])
                return "merged"
            raise AssertionError(args)
        worker.api, worker.gh = api, gh
        def native(directory, entry, sha, stage):
            self.assertEqual(d.git(directory / "checkout", "rev-parse", "HEAD"), sha)
            return dict(sha=sha, results=[dict(status="passed", command=["controlled native checks"])])
        worker.native = native
        return prs, counts

    def test_full_advance_and_mutation_crash_recovery(self):
        # Each crash leaves external facts intact and discards only the in-memory job.
        for crash in (None, "push", "code-pr", "code-merge", "mark", "tracking-commit", "tracking-pr", "tracking-merge"):
            with self.subTest(crash=crash):
                case = DeliveryTest()
                case.setUp()
                try:
                    directory, job, evidence = case.ticket_fixture(new_plan=True)
                    import shutil
                    shutil.rmtree(directory / "checkout")
                    # Normal cleanup may delete the source branch or advance another branch.
                    d.git(case.repo, "checkout", "-b", "next-story")
                    d.git(case.repo, "branch", "-D", "main")
                    job["source_branch"] = "main"
                    worker = d.Worker(case.cfg)
                    prs, counts = case.transport(worker, job)
                    original_run, original_gh = d.run, worker.gh
                    fired = []
                    def interrupt(point):
                        if crash == point and not fired:
                            fired.append(point)
                            raise KeyboardInterrupt("simulated worker termination")
                    def run(args, *a, **kw):
                        result = original_run(args, *a, **kw)
                        if args[:2] == ["git", "push"]:
                            interrupt("push")
                        if args[0] == d.sys.executable and "mark" in args:
                            interrupt("mark")
                        if args[:2] == ["git", "commit"]:
                            interrupt("tracking-commit")
                        return result
                    def gh(*args):
                        result = original_gh(*args)
                        if args[:2] == ("pr", "create"):
                            interrupt("tracking-pr" if args[args.index("--head") + 1].endswith("-tracking") else "code-pr")
                        if args[:2] == ("pr", "merge"):
                            interrupt("tracking-merge" if prs[int(args[2])]["head"]["ref"].endswith("-tracking") else "code-merge")
                        return result
                    worker.gh = gh
                    with patch.object(d, "run", side_effect=run):
                        try:
                            worker.advance(directory, job)
                        except KeyboardInterrupt:
                            pass
                    worker.gh = original_gh
                    worker.advance(directory, d.read(directory / "job.json"))
                    final = d.read(directory / "job.json")
                    self.assertEqual(final["stage"], "complete")
                    self.assertEqual(counts, dict(code=1, tracking=1))
                    self.assertEqual(len(prs), 2)
                    self.assertEqual(d.git(case.repo, "status", "--porcelain"), "")
                    self.assertIn("status: built", (case.repo / job["plan"]).read_text())
                    self.assertIn("status: done", d.git(directory / "checkout", "show", f"{final['tracking_sha']}:{job['plan']}"))
                    self.assertEqual((case.repo / "source").read_text(), "initial")
                    if crash:
                        self.assertTrue(fired, crash)
                finally:
                    case.doCleanups()

    def test_newer_plan_changes_cannot_be_accepted_by_old_job(self):
        directory, job, evidence = self.ticket_fixture()
        worker = d.Worker(self.cfg)
        job["code_pr"] = dict(merged=True)
        original = (self.repo / job["plan"]).read_text()
        for changed in (original.replace("status: built", "status: in-progress"),
                        original.replace("status: built", "status: in-review"),
                        original.replace("title: Test", "title: Test\nbaseline_revision: new"),
                        original + "\n## Verification\nNew review evidence.\n"):
            (self.repo / job["plan"]).write_text(changed)
            d.git(self.repo, "add", ".")
            d.git(self.repo, "commit", "-m", "new plan change")
            d.git(self.repo, "push", "origin", "main")
            with self.assertRaisesRegex(d.Pending, "native plan"):
                worker.current_ticket(directory, job, job["sha"])

    def test_evidence_cli_initializes_clone_before_first_worker(self):
        directory, job, evidence = self.ticket_fixture()
        import shutil
        shutil.rmtree(directory / "checkout")
        cfg_path = self.root / "config.json"
        d.atomic(cfg_path, self.cfg)
        submitted = self.root / "submitted.json"
        d.atomic(submitted, evidence)
        d.run([d.sys.executable, "-B", str(Path(d.__file__).resolve()), "--config", str(cfg_path),
               "evidence", job["id"], str(submitted)])
        self.assertTrue((directory / "checkout/.git").exists())
        self.assertEqual(d.read(directory / "job.json")["status"], "queued")

    def test_worker_polls_new_jobs_and_releases_lock(self):
        directory, job = self.queued()
        cfg_path = self.root / "config.json"
        d.atomic(cfg_path, dict(self.cfg, poll_interval=0, worker_lifetime=2))
        calls = []
        def pass_once(worker):
            calls.append(1)
            entry = d.read(directory / "job.json")
            if len(calls) == 1:
                entry["status"] = "waiting"
            else:
                entry.update(stage="complete", status="complete")
            worker.save(directory, entry)
        with patch.object(d.sys, "argv", ["delivery", "--config", str(cfg_path), "worker"]), patch.object(d.Worker, "pass_once", pass_once):
            with d.lock(Path(self.cfg["state_dir"]) / "worker.lock"):
                d.main()
            self.assertEqual(calls, [])
            d.main()
            self.assertEqual(len(calls), 2)
            d.main()
            self.assertEqual(len(calls), 3)
        with d.lock(Path(self.cfg["state_dir"]) / "worker.lock") as held:
            self.assertTrue(held)

    def test_waiting_delivery_does_not_block_real_runnable_delivery(self):
        directory, job, evidence = self.ticket_fixture()
        worker = d.Worker(self.cfg)
        case = DeliveryTest()
        case.setUp()
        try:
            other, runnable, evidence = case.ticket_fixture()
            # Share only the queue, never the checkout or run binding.
            import shutil
            waiting = other.parent / job["id"]
            shutil.copytree(directory, waiting)
            waiting_evidence = d.read(waiting / "evidence.json")
            waiting_evidence["operator_gates"] = ["pending criterion"]
            d.atomic(waiting / "evidence.json", waiting_evidence)
            runnable_worker = d.Worker(case.cfg)
            case.transport(runnable_worker, runnable)
            runnable_worker.pass_once()
            self.assertEqual(d.read(waiting / "job.json")["status"], "waiting")
            self.assertEqual(d.read(other / "job.json")["stage"], "complete")
        finally:
            case.doCleanups()

    def test_confirmation_rejects_wrong_destination_and_merge_sha(self):
        worker = d.Worker(self.cfg)
        job = dict(repository="owner/repo", base="main")
        full = dict(merged=True, merge_commit_sha=self.sha, head=dict(sha=self.sha),
                    base=dict(ref="main", repo=dict(full_name="owner/repo")))
        for bad in (dict(full, merge_commit_sha="invalid"), dict(full, base=dict(ref="other", repo=dict(full_name="owner/repo"))),
                    dict(full, base=dict(ref="main", repo=dict(full_name="other/repo")))):
            with self.assertRaisesRegex(d.Pending, "identity"):
                worker.confirm_merge(job, self.sha, bad)

    def test_real_pull_requires_description_and_verify_evidence(self):
        directory, job, evidence = self.ticket_fixture()
        script = ".agents/skills/bmad-agent-po/scripts/tickets.py"
        entry = self.repo / evidence["contract"]["path"]
        entry.write_text(entry.read_text().replace('## Acceptance Criteria\\n\\n1. Given a job, delivery completes.',
            'Deliver the ordinary entry.').replace('hitl = false', 'verify = "Verify isolated delivery."\nhitl = false'))
        runtime = self.repo / "_bmad/custom"
        runtime.mkdir(parents=True)
        (runtime / "config.user.toml").write_text('[core]\nticket_store = "repo"\n')
        with (self.repo / ".git/info/exclude").open("a") as stream:
            stream.write("\n_bmad/\n")
        result = json.loads(d.run([d.sys.executable, script, "--project-root", str(self.repo),
            "pull", "initiative/tickets/epic-test", "1"], self.repo))
        story = entry.parent / result["file"]
        text = story.read_text()
        self.assertIn("## Description\n\nDeliver the ordinary entry.", text)
        self.assertIn("## Requirements\n\nVerify: Verify isolated delivery.", text)
        d.git(self.repo, "add", ".")
        d.git(self.repo, "commit", "-m", "real pulled story")
        self.sha = d.git(self.repo, "rev-parse", "HEAD")
        self.state["tasks"]["1.1"]["commit_sha"] = self.sha
        d.atomic(self.run_dir / "state.json", self.state)
        directory, job = self.queued()
        worker = d.Worker(self.cfg)
        worker.initialize(directory, job)
        evidence.update({k: job[k] for k in ("repository", "run", "ticket", "sha", "base", "container")})
        evidence["review"]["sha"] = self.sha
        evidence["contract"]["path"] = str(story.relative_to(self.repo))
        evidence["artifacts"] = [dict(path=p, sha256=hashlib.sha256((self.repo / p).read_bytes()).hexdigest())
            for p in ("review.md", str(story.relative_to(self.repo)), job["plan"])]
        evidence["criteria"] = [dict(criterion=c, status="passed", artifact="review.md")
            for c in ("Deliver the ordinary entry.", "Verify: Verify isolated delivery.")]
        d.atomic(directory / "evidence.json", evidence)
        self.assertEqual(worker.evidence(directory, job), evidence)
        evidence["criteria"] = evidence["criteria"][1:]
        d.atomic(directory / "evidence.json", evidence)
        with self.assertRaisesRegex(d.Pending, "every contract"):
            worker.evidence(directory, job)

    def add_gate(self, gate):
        entry = self.repo / "initiative/tickets/epic-test/tickets/tickets.toml"
        if gate in ("after", "review-after"):
            entry.write_text(entry.read_text().replace("hitl = false", "hitl = false\nafter = [2]") +
                '\n[[entry]]\nid = 2\ntype = "story"\ntitle = "Prerequisite"\ndescription = "Required before delivery."\n')
            if gate == "review-after":
                (entry.parent / "story-prerequisite-plan.md").write_text("---\ntitle: Prerequisite\nticket: 2\nstatus: built\n---\n")
        elif gate == "unknown":
            entry.write_text(entry.read_text().replace("hitl = false", 'hitl = false\nunknown = "Owner input required."'))
        else:
            initiative = self.repo / "initiative/tickets/tickets.toml"
            initiative.write_text(initiative.read_text() + '\n[[epic]]\nid = 2\nslug = "epic-required"\ntitle = "Required"\n')
            epic = self.repo / "initiative/tickets/epic-required"
            (epic / "tickets").mkdir(parents=True)
            (epic / "epic-required.md").write_text("---\ntype: epic\ntitle: Required\nparent: initiative\n---\n")
            (epic / "tickets/tickets.toml").write_text("")
            target = self.repo / "initiative/tickets/epic-test/epic-test.md"
            target.write_text(target.read_text().replace("parent: initiative", "parent: initiative\nafter: [epic-required]"))
        d.git(self.repo, "add", ".")
        d.git(self.repo, "commit", "-m", "add prerequisite gate")
        d.git(self.repo, "push", "origin", "main")
        return d.git(self.repo, "rev-parse", "HEAD")

    def test_new_dependency_epic_and_unknown_gates_prevent_merge_and_mark(self):
        for gate in ("after", "gated_by", "unknown"):
            for merged in (False, True):
                with self.subTest(gate=gate, merged=merged):
                    case = DeliveryTest()
                    case.setUp()
                    try:
                        directory, job, evidence = case.ticket_fixture()
                        case.add_gate(gate)
                        if merged:
                            job.update(stage="tracking", code_pr=dict(merged=True))
                        worker = d.Worker(case.cfg)
                        case.transport(worker, job)
                        original_run = d.run
                        mutations = []
                        def run(args, *a, **kw):
                            if "mark" in args or args[:2] == ["git", "push"]:
                                mutations.append(args)
                            return original_run(args, *a, **kw)
                        with patch.object(d, "run", side_effect=run), patch.object(worker, "gh", wraps=worker.gh) as gh:
                            with self.assertRaisesRegex(d.Pending, "gates"):
                                worker.advance(directory, job)
                            self.assertFalse(any(call.args[:2] == ("pr", "merge") for call in gh.call_args_list))
                        self.assertEqual(mutations, [])
                    finally:
                        case.doCleanups()

    def test_native_review_dependency_is_met_but_drift_still_blocks(self):
        directory, job, evidence = self.ticket_fixture()
        sha = self.add_gate("review-after")
        checkout = directory / "checkout"
        d.git(checkout, "fetch", "origin", "main")
        found = d.Worker(self.cfg).ticket(checkout, job, sha)
        self.assertEqual(found["after"], [2])
        with self.assertRaisesRegex(d.Pending, "contract or gates changed"):
            d.Worker(self.cfg).current_ticket(directory, job, job["sha"])

    def test_hooks_survive_source_worktree_deletion(self):
        for layout in ("relative", "default"):
            with self.subTest(layout=layout):
                case = DeliveryTest()
                case.setUp()
                try:
                    hook = case.repo / ("tracked-hooks/pre-push" if layout == "relative" else ".git/hooks/pre-push")
                    hook.parent.mkdir(exist_ok=True)
                    marker = case.root / "hook-ran"
                    hook.write_text(f"#!/bin/sh\nprintf 'ran' >> '{marker}'\n")
                    hook.chmod(0o700)
                    if layout == "relative":
                        d.git(case.repo, "config", "core.hooksPath", "tracked-hooks")
                        d.git(case.repo, "add", ".")
                        d.git(case.repo, "commit", "-m", "tracked push hook")
                    case.sha = d.git(case.repo, "rev-parse", "HEAD")
                    worktree = case.root / "removed-worktree"
                    d.git(case.repo, "worktree", "add", "--detach", str(worktree), case.sha)
                    case.state["tasks"]["1.1"]["commit_sha"] = case.sha
                    d.atomic(case.run_dir / "state.json", case.state)
                    key = d.enqueue(case.cfg, worktree, case.run_dir, "1.1")
                    directory = Path(case.cfg["state_dir"]) / key
                    job = d.read(directory / "job.json")
                    d.git(case.repo, "worktree", "remove", str(worktree))
                    worker = d.Worker(case.cfg)
                    worker.initialize(directory, job)
                    checkout = directory / "checkout"
                    d.git(checkout, "checkout", "--detach", case.sha)
                    worker.pr = lambda *a: None
                    worker.current_ticket = lambda *a: None
                    worker.native = lambda *a: dict(sha=case.sha)
                    worker.gh = lambda *a: (_ for _ in ()).throw(d.Pending("stop after actual push"))
                    with self.assertRaisesRegex(d.Pending, "after actual push"):
                        worker.deliver(directory, job, case.sha, "hook-test", "code")
                    self.assertEqual(marker.read_text(), "ran")
                    effective = Path(d.git(checkout, "rev-parse", "--path-format=absolute", "--git-path", "hooks/pre-push"))
                    effective.chmod(0o600)
                    with self.assertRaisesRegex(d.Pending, "not executable"):
                        worker.deliver(directory, job, case.sha, "hook-missing", "code")
                    effective.unlink()
                    with self.assertRaisesRegex(d.Pending, "absent"):
                        worker.deliver(directory, job, case.sha, "hook-missing", "code")
                finally:
                    case.doCleanups()

    def test_api_base_branch_is_encoded(self):
        worker = self.checker(False, protected=True)
        calls = []
        original = worker.api
        worker.api = lambda endpoint: calls.append(endpoint) or original(endpoint)
        worker.checks(dict(repository="owner/repo"), self.sha, "feature/foo&bar")
        self.assertIn("repos/owner/repo/branches/feature%2Ffoo%26bar", calls)
        self.assertIn("repos/owner/repo/branches/feature%2Ffoo%26bar/protection", calls)
        worker.pages = lambda endpoint: calls.append(endpoint) or []
        worker.pr(dict(repository="owner/repo", base="feature/foo&bar"), "delivery/code")
        self.assertTrue(calls[-1].endswith("&base=feature%2Ffoo%26bar"))

    def test_repository_environment_is_removed_with_explicit_or_inherited_env(self):
        expected = set(d.git(self.repo, "rev-parse", "--local-env-vars").splitlines())
        self.assertLessEqual(expected, d.GIT_LOCAL_ENV)
        poison = {key: "poisoned" for key in expected}
        retained = dict(PATH=os.environ["PATH"], SSH_AUTH_SOCK="ssh-socket", GIT_SSH_COMMAND="ssh -v",
                        WXT_PREMIUM_API_URL="http://localhost:8787", CI="true")
        code = "import json, os; print(json.dumps(dict(os.environ)))"
        with patch.dict(os.environ, dict(poison, **retained)):
            for env in (None, dict(os.environ)):
                result = json.loads(d.run([d.sys.executable, "-c", code], env=env))
                self.assertFalse(expected.intersection(result))
                for key, value in retained.items():
                    self.assertEqual(result[key], value)

    @unittest.skipIf(os.environ.get("DELIVERY_NESTED_GIT_TEST") == "1", "outer test runs this suite under hook environment")
    def test_nested_suite_under_hook_environment_preserves_parent_repositories(self):
        owning = ROOT
        def snapshot(root):
            common = Path(d.git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
            index = Path(d.git(root, "rev-parse", "--path-format=absolute", "--git-path", "index"))
            return dict(config=(common / "config").read_bytes(), index=index.read_bytes(),
                        head=d.git(root, "rev-parse", "HEAD"), status=d.git(root, "status", "--porcelain"),
                        name=d.git(root, "config", "user.name"), email=d.git(root, "config", "user.email"))
        before = {str(root): snapshot(root) for root in (owning, self.repo)}
        poisoned = dict(os.environ, GIT_DIR=str(self.repo / ".git"), GIT_COMMON_DIR=str(self.repo / ".git"),
                        GIT_INDEX_FILE=str(self.repo / ".git/index"), GIT_WORK_TREE=str(self.repo),
                        GIT_PREFIX="poisoned/", DELIVERY_NESTED_GIT_TEST="1")
        with patch.dict(os.environ, poisoned):
            # Exercise writes first, with both inherited and explicitly supplied environments.
            child = self.root / "child"
            child.mkdir()
            d.git(child, "init", "-b", "main")
            d.run(["git", "config", "user.name", "Nested disposable"], child, env=poisoned)
            self.assertEqual(d.git(child, "config", "user.name"), "Nested disposable")
        # Launch without run(): the suite itself must enter with Git's hook variables intact.
        with tempfile.TemporaryFile() as output:
            result = subprocess.run([d.sys.executable, "-B", "-m", "unittest", "discover", "-s",
                str(Path(__file__).resolve().parent), "-p", "test_*.py"], cwd=owning, env=poisoned,
                stdout=output, stderr=subprocess.STDOUT, timeout=300)
            output.seek(0)
            log = output.read().decode()
        self.assertEqual(result.returncode, 0, log[-8000:])
        self.assertIn("OK (skipped=1)", log)
        for root in (owning, self.repo):
            self.assertEqual(snapshot(root), before[str(root)])

    def test_config_permissions(self):
        path = self.root / "config.json"
        d.atomic(path, self.cfg)
        os.chmod(path, 0o666)
        with self.assertRaises(ValueError):
            d.config(path)

    def test_command_timeout_kills_group(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            d.run(["python3", "-c", "import time; time.sleep(10)"], timeout=.01)


if __name__ == "__main__":
    unittest.main()
