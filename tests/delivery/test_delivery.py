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
        d.git(self.repo, "worktree", "add", "-b", "isolated-story", str(worktree), self.sha)
        directory = d.enqueue(self.cfg, worktree, self.run_dir, "1.1", self.sha, "done")
        job = d.read(Path(self.cfg["state_dir"]) / directory / "job.json")
        self.assertEqual(job["plan"], "initiative/plan.md")
        self.assertEqual(job["source_branch"], "isolated-story")

    def test_inplace_enqueue_captures_actual_branch_without_changing_run(self):
        d.git(self.repo, "switch", "-c", "story")
        self.state["tasks"]["1.1"].update(branch="", target_branch="main")
        d.atomic(self.run_dir / "state.json", self.state)
        before = (self.run_dir / "state.json").read_bytes()
        _, job = self.queued()
        self.assertEqual(job["source_branch"], "story")
        self.assertEqual(job["sha"], self.sha)
        self.assertEqual((self.run_dir / "state.json").read_bytes(), before)

    def test_missing_branch_rejects_detached_or_mismatched_head_without_pin(self):
        d.git(self.repo, "checkout", "--detach", self.sha)
        with self.assertRaisesRegex(d.Pending, "attached HEAD"):
            self.queued()
        d.git(self.repo, "switch", "main")
        d.git(self.repo, "commit", "--allow-empty", "-m", "advance source")
        with self.assertRaisesRegex(d.Pending, "pinned commit"):
            self.queued()
        self.assertEqual(d.git(self.repo, "for-each-ref", "refs/delivery/"), "")
        self.assertEqual(list(Path(self.cfg["state_dir"]).glob("*/job.json")), [])

    def test_source_branch_is_not_ambiguous_with_same_named_tag(self):
        d.git(self.repo, "switch", "-c", "story/nested")
        d.git(self.repo, "tag", "story/nested")
        _, job = self.queued()
        self.assertEqual(job["source_branch"], "story/nested")

    def test_branch_switch_during_capture_is_rejected_before_pin(self):
        original = d.git
        def git(root, *args):
            value = original(root, *args)
            if args == ("rev-parse", "HEAD"):
                original(root, "switch", "-c", "next-story")
            return value
        with patch.object(d, "git", side_effect=git), self.assertRaisesRegex(d.Pending, "pinned commit"):
            self.queued()
        self.assertEqual(d.git(self.repo, "for-each-ref", "refs/delivery/"), "")
        self.assertEqual(list(Path(self.cfg["state_dir"]).glob("*/job.json")), [])

    def test_branch_movement_at_pin_transaction_cannot_create_job_or_pin(self):
        original = d.subprocess.Popen
        def popen(args, *a, **kw):
            if args == ["git", "update-ref", "--stdin"]:
                d.git(self.repo, "commit", "--allow-empty", "-m", "concurrent advance")
            return original(args, *a, **kw)
        with patch.object(d.subprocess, "Popen", side_effect=popen), self.assertRaisesRegex(RuntimeError, "cannot lock ref"):
            self.queued()
        self.assertEqual(d.git(self.repo, "for-each-ref", "refs/delivery/"), "")
        self.assertEqual(list(Path(self.cfg["state_dir"]).glob("*/job.json")), [])

    def test_head_switch_or_detach_at_pin_transaction_is_atomic(self):
        for linked in (False, True):
            for action in ("switch", "detach"):
                with self.subTest(linked=linked, action=action):
                    case = DeliveryTest()
                    case.setUp()
                    try:
                        source = case.repo
                        if linked:
                            source = case.root / "worktree"
                            d.git(case.repo, "worktree", "add", "-b", "story", str(source), case.sha)
                        before = (case.run_dir / "state.json").read_bytes()
                        original = d.subprocess.Popen
                        def popen(args, *a, **kw):
                            if args == ["git", "update-ref", "--stdin"]:
                                command = (["switch", "-c", "next-story"] if action == "switch"
                                           else ["checkout", "--detach", case.sha])
                                d.git(source, *command)
                            return original(args, *a, **kw)
                        with patch.object(d.subprocess, "Popen", side_effect=popen), self.assertRaises(d.Pending):
                            d.enqueue(case.cfg, source, case.run_dir, "1.1")
                        self.assertEqual(d.git(case.repo, "for-each-ref", "refs/delivery/"), "")
                        self.assertEqual(list(Path(case.cfg["state_dir"]).glob("*/job.json")), [])
                        self.assertEqual((case.run_dir / "state.json").read_bytes(), before)
                    finally:
                        case.doCleanups()

    def test_unsupported_transaction_fails_without_job_or_pin(self):
        for rejected in ("start", "prepare"):
            with self.subTest(command=rejected):
                case = DeliveryTest()
                case.setUp()
                try:
                    original = d.subprocess.Popen
                    def popen(args, *a, **kw):
                        if args == ["git", "update-ref", "--stdin"]:
                            code = ("import sys\nfor line in sys.stdin:\n"
                                    f" if line.strip() == {rejected!r}:\n"
                                    "  sys.stderr.write('fatal: unknown command: ' + line); sys.exit(128)\n"
                                    " if line.strip() == 'start': print('start: ok', flush=True)\n")
                            args = [d.sys.executable, "-c", code]
                        return original(args, *a, **kw)
                    with patch.object(d.subprocess, "Popen", side_effect=popen), self.assertRaisesRegex(RuntimeError, "unknown command"):
                        case.queued()
                    self.assertEqual(d.git(case.repo, "for-each-ref", "refs/delivery/"), "")
                    self.assertEqual(list(Path(case.cfg["state_dir"]).glob("*/job.json")), [])
                    case.queued()
                finally:
                    case.doCleanups()

    def test_prepared_pin_locks_head_and_tip_until_identity_check(self):
        for linked in (False, True):
            with self.subTest(linked=linked):
                case = DeliveryTest()
                case.setUp()
                try:
                    source, branch = case.repo, "main"
                    if linked:
                        source, branch = case.root / "worktree", "story"
                        d.git(case.repo, "worktree", "add", "-b", branch, str(source), case.sha)
                    original = d.git
                    checked = []
                    def git(root, *args):
                        if args == ("symbolic-ref", "--no-recurse", "--quiet", "HEAD"):
                            for command in (("symbolic-ref", "HEAD", "refs/heads/other"),
                                            ("update-ref", "refs/heads/" + branch, case.sha)):
                                with self.assertRaisesRegex(RuntimeError, "lock"):
                                    original(root, "-c", "core.filesRefLockTimeout=0", *command)
                            if linked:
                                original(case.repo, "symbolic-ref", "HEAD", "refs/heads/other-main")
                            checked.append(True)
                        return original(root, *args)
                    with patch.object(d, "git", side_effect=git):
                        key = d.enqueue(case.cfg, source, case.run_dir, "1.1")
                    job = d.read(Path(case.cfg["state_dir"]) / key / "job.json")
                    self.assertEqual(checked, [True])
                    self.assertEqual(job["source_branch"], branch)
                finally:
                    case.doCleanups()

    def test_prepared_pin_timeout_aborts_and_releases_locks(self):
        with patch.object(d.concurrent.futures.Future, "result", side_effect=TimeoutError("controlled read timeout")):
            with self.assertRaises(TimeoutError):
                self.queued()
        self.assertEqual(d.git(self.repo, "for-each-ref", "refs/delivery/"), "")
        self.assertEqual(list(Path(self.cfg["state_dir"]).glob("*/job.json")), [])
        # A retry must be able to acquire both Git locks after the abort.
        self.queued()

    def test_hung_transaction_hooks_release_locks_and_confirm_committed_pin(self):
        for stage in ("prepared", "committed", "ignore-term"):
            with self.subTest(stage=stage):
                case = DeliveryTest()
                case.setUp()
                try:
                    hook = case.repo / ".git/hooks/reference-transaction"
                    pid_file = case.root / "hook.pid"
                    if stage == "ignore-term":
                        hook.write_text(f'#!/bin/sh\nif [ "$1" = "prepared" ]; then trap "" TERM; echo $$ > "{pid_file}"; sleep 60; fi\nexit 0\n')
                    else:
                        hook.write_text(f'#!/bin/sh\nif [ "$1" = "{stage}" ]; then echo $$ > "{pid_file}"; sleep 60; fi\nexit 0\n')
                    hook.chmod(0o700)
                    result = d.concurrent.futures.Future.result
                    communicate = d.subprocess.Popen.communicate
                    def wait_for_hook():
                        deadline = d.time.monotonic() + 10
                        while not pid_file.exists() and d.time.monotonic() < deadline:
                            d.time.sleep(0.01)
                        self.assertTrue(pid_file.exists(), "transaction hook did not start")
                    def fast_result(future, timeout=None):
                        if stage != "committed":
                            wait_for_hook()
                        return result(future, timeout=0.3 if stage != "committed" else 10)
                    def fast_communicate(process, *a, **kw):
                        if a == ("commit\n",):
                            process.stdin.write(a[0])
                            process.stdin.flush()
                            wait_for_hook()
                            a = ()
                        return communicate(process, *a, **dict(kw, timeout=0.1))
                    with patch.object(d.concurrent.futures.Future, "result", fast_result), patch.object(d.subprocess.Popen, "communicate", fast_communicate):
                        if stage != "committed":
                            with self.assertRaises(TimeoutError):
                                case.queued()
                        else:
                            directory, job = case.queued()
                            self.assertEqual(d.read(directory / "job.json")["sha"], case.sha)
                            self.assertEqual(d.git(case.repo, "rev-parse", "refs/delivery/" + job["id"]), case.sha)
                    self.assertEqual(list((case.repo / ".git").rglob("*.lock")), [])
                    if stage == "ignore-term":
                        pid = int(pid_file.read_text())
                        # A killed hook can remain briefly as a zombie, but must not run.
                        result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
                        self.assertTrue(not result.stdout.strip() or result.stdout.strip().startswith("Z"), result.stdout)
                    if stage != "committed":
                        self.assertEqual(d.git(case.repo, "for-each-ref", "refs/delivery/"), "")
                        self.assertEqual(list(Path(case.cfg["state_dir"]).glob("*/job.json")), [])
                    hook.unlink()
                    case.queued()
                finally:
                    case.doCleanups()

    def test_post_commit_captures_inplace_source_with_stale_saved_state(self):
        from types import SimpleNamespace
        self.state["tasks"]["1.1"].update(branch="", phase="committing", commit_sha=None)
        d.atomic(self.run_dir / "state.json", self.state)
        before = (self.run_dir / "state.json").read_bytes()
        cfg_path = self.root / "config.json"
        d.atomic(cfg_path, self.cfg)
        plugin = d.DeliveryPlugin(None, dict(config=str(cfg_path)))
        ctx = SimpleNamespace(stage="post_commit", phase="done", worktree=None,
                              repo_root=str(self.repo), run_dir=str(self.run_dir), story_key="1.1", shared={})
        # Keep real Git Popen calls; intercept only the worker launch.
        original = d.subprocess.Popen
        launches = []
        def popen(args, *a, **kw):
            if args[0] == d.sys.executable:
                launches.append(args)
                return None
            return original(args, *a, **kw)
        with patch.object(d.subprocess, "Popen", side_effect=popen):
            plugin.capture(ctx)
        self.assertEqual(ctx.shared, {})
        jobs = list(Path(self.cfg["state_dir"]).glob("*/job.json"))
        self.assertEqual(len(jobs), 1)
        job = d.read(jobs[0])
        self.assertEqual((job["source_branch"], job["sha"]), ("main", self.sha))
        self.assertEqual(len(launches), 1)
        self.assertEqual((self.run_dir / "state.json").read_bytes(), before)
        # A fresh run on detached HEAD must record an advisory failure only.
        ctx.run_dir = str(self.root / "detached-run")
        d.atomic(Path(ctx.run_dir) / "state.json", self.state)
        d.git(self.repo, "checkout", "--detach", self.sha)
        pins = d.git(self.repo, "for-each-ref", "refs/delivery/")
        with patch.object(d.subprocess, "Popen", side_effect=popen):
            plugin.capture(ctx)
        self.assertIn("attached HEAD", ctx.shared["delivery_error"])
        self.assertEqual(len(launches), 1)
        self.assertEqual(list(Path(self.cfg["state_dir"]).glob("*/job.json")), jobs)
        self.assertEqual(d.git(self.repo, "for-each-ref", "refs/delivery/"), pins)
        self.assertEqual((Path(ctx.run_dir) / "state.json").read_bytes(), before)

    def test_redirected_descendants_after_graceful_abort_or_early_git_exit(self):
        for outcome in ("graceful-abort", "early-exit", "success"):
            with self.subTest(outcome=outcome):
                case = DeliveryTest()
                case.setUp()
                pid_file, release = case.root / "child.pid", case.root / "release-hook"
                try:
                    hook = case.repo / ".git/hooks/reference-transaction"
                    wait = (f'while [ ! -e "{release}" ]; do sleep 0.01; done\n'
                            if outcome != "success" else "")
                    hook.write_text('#!/bin/sh\nif [ "$1" = "prepared" ]; then\n'
                                    f'sleep 60 >/dev/null 2>&1 &\necho $! > "{pid_file}"\n'
                                    + wait + ('exit 1\n' if outcome == "early-exit" else '') + 'fi\nexit 0\n')
                    hook.chmod(0o700)
                    popen, result, killpg = d.subprocess.Popen, d.concurrent.futures.Future.result, d.os.killpg
                    transaction, signals, reads = [], [], []
                    def start(args, *a, **kw):
                        process = popen(args, *a, **kw)
                        if args == ["git", "update-ref", "--stdin"]:
                            transaction.append(process)
                        return process
                    def read(future, timeout=None):
                        reads.append(True)
                        if len(reads) != 2 or outcome == "success":
                            return result(future, timeout=10)
                        deadline = d.time.monotonic() + 10
                        while not pid_file.exists() and d.time.monotonic() < deadline:
                            d.time.sleep(0.01)
                        self.assertTrue(pid_file.exists(), "prepared hook did not start")
                        try:
                            return result(future, timeout=0.05)
                        except TimeoutError:
                            release.touch()
                            if outcome == "early-exit":
                                transaction[0].wait(timeout=10)
                            raise
                    def stop(group, sig):
                        signals.append(sig)
                        return killpg(group, sig)
                    with patch.object(d.subprocess, "Popen", side_effect=start), patch.object(d.concurrent.futures.Future, "result", read), patch.object(d.os, "killpg", side_effect=stop):
                        if outcome == "success":
                            case.queued()
                        else:
                            with self.assertRaises(TimeoutError):
                                case.queued()
                    pid = int(pid_file.read_text())
                    status = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
                    if outcome == "success":
                        self.assertTrue(status and not status.startswith("Z"), status)
                        self.assertEqual(signals, [])
                    else:
                        self.assertTrue(not status or status.startswith("Z"), status)
                        self.assertEqual(signals, [d.signal.SIGKILL])
                        self.assertEqual(d.git(case.repo, "for-each-ref", "refs/delivery/"), "")
                        self.assertEqual(list(Path(case.cfg["state_dir"]).glob("*/job.json")), [])
                    self.assertEqual(list((case.repo / ".git").rglob("*.lock")), [])
                    hook.unlink()
                    case.queued()
                finally:
                    if pid_file.exists():
                        with d.contextlib.suppress(ProcessLookupError):
                            os.kill(int(pid_file.read_text()), d.signal.SIGKILL)
                    case.doCleanups()

    def test_missing_branch_rejects_foreign_repository_with_same_commit(self):
        other = self.root / "other"
        d.git(self.repo, "clone", str(self.repo), str(other))
        with self.assertRaisesRegex(d.Pending, "run repository"):
            d.enqueue(self.cfg, other, self.run_dir, "1.1")
        self.assertEqual(d.git(other, "for-each-ref", "refs/delivery/"), "")
        self.assertEqual(list(Path(self.cfg["state_dir"]).glob("*/job.json")), [])

    def test_existing_job_is_unchanged_after_source_moves_or_detaches(self):
        directory, job = self.queued()
        # A legacy job must not acquire a branch during a repeated enqueue.
        job["source_branch"] = ""
        d.atomic(directory / "job.json", job)
        before = (directory / "job.json").read_bytes()
        d.git(self.repo, "commit", "--allow-empty", "-m", "next story")
        for detached in (False, True):
            if detached:
                d.git(self.repo, "checkout", "--detach")
            self.assertEqual(d.enqueue(self.cfg, self.repo, self.run_dir, "1.1"), job["id"])
            self.assertEqual((directory / "job.json").read_bytes(), before)

    def test_explicit_branch_remains_supported_with_detached_checkout(self):
        self.state["tasks"]["1.1"]["branch"] = "isolated-story"
        d.atomic(self.run_dir / "state.json", self.state)
        d.git(self.repo, "checkout", "--detach", self.sha)
        _, job = self.queued()
        self.assertEqual(job["source_branch"], "isolated-story")

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

    def transport(self, worker, job, prs=None, squash=False):
        """Controlled gh; mutations still push and merge real disposable Git refs."""
        # Local bare remotes replace GitHub only in this controlled transport.
        identity = patch.object(d, "github_origin", return_value="https://github.com/owner/repo")
        identity.start()
        self.addCleanup(identity.stop)
        prs = {} if prs is None else prs
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
                    base=dict(ref=job["base"], repo=dict(full_name="owner/repo")))
                return str(number)
            pr = prs[int(args[2])]
            if args[:2] == ("pr", "view"):
                return json.dumps(dict(headRefOid=pr["head"]["sha"], baseRefName=pr["base"]["ref"],
                    mergeStateStatus="CLEAN", reviewDecision="APPROVED", isDraft=False))
            if args[:2] == ("pr", "merge"):
                self.assertEqual(args[args.index("--match-head-commit") + 1], pr["head"]["sha"])
                merged = pr["head"]["sha"]
                base_ref = f"refs/heads/{pr['base']['ref']}"
                if squash:
                    tree = d.git(self.repo, "--git-dir", str(self.remote), "rev-parse", merged + "^{tree}")
                    parent = d.git(self.repo, "--git-dir", str(self.remote), "rev-parse", base_ref)
                    merged = d.git(self.repo, "-c", "user.name=Disposable Test", "-c", "user.email=test@example.invalid",
                                   "--git-dir", str(self.remote), "commit-tree", tree, "-p", parent,
                                   "-m", f"Squash controlled PR {pr['number']}")
                d.git(self.repo, "--git-dir", str(self.remote), "update-ref", base_ref, merged)
                pr.update(state="closed", merged=True, merged_at="now", merge_commit_sha=merged)
                return "merged"
            raise AssertionError(args)
        worker.api, worker.gh = api, gh
        def native(directory, entry, sha, stage):
            self.assertEqual(d.git(directory / "checkout", "rev-parse", "HEAD"), sha)
            return dict(sha=sha, platform="darwin", results=[dict(status="passed", command=c) for c in
                        (["pnpm", "install", "--frozen-lockfile"], ["pnpm", "lint"], ["pnpm", "typecheck"], ["pnpm", "test"])])
        worker.native = native
        return prs, counts

    def integration_fixture(self, multiple=False, transient=False, base_gate=False, child_fields="", child_baseline=False, child_crlf=False, inplace=False, legacy_child=False):
        """Accept a child on integration through two real squash merge commits."""
        self.ticket_fixture(new_plan=not child_baseline)
        child_plan = self.repo / "initiative/tickets/epic-test/tickets/story-test-plan.md"
        if child_baseline:
            child_plan.write_text(child_plan.read_text().replace("status: built", "status: draft"))
            d.git(self.repo, "add", ".")
            d.git(self.repo, "commit", "-m", "original child draft plan")
            d.git(self.repo, "push", "origin", "main")
            d.git(self.repo, "checkout", "-b", "story")
            child_plan.write_text(child_plan.read_text().replace("status: draft", "status: built"))
            d.git(self.repo, "add", ".")
            d.git(self.repo, "commit", "-m", "build child from original draft")
        entry_path = "initiative/tickets/epic-test/tickets/tickets.toml"
        d.git(self.repo, "checkout", "main")
        entry = self.repo / entry_path
        entry.write_text(entry.read_text() + '\n[[entry]]\nid = 2\ntype = "story"\ntitle = "Final"\n'
                         'description = "Given accepted children, final delivery completes."\n'
                         + ('after = ["1.1", "1.3"]\n\n[[entry]]\nid = 3\ntype = "story"\ntitle = "Second"\n'
                            'description = "Accept the second child."\nafter = ["1.1"]\n' if multiple else 'after = ["1.1"]\n'))
        if base_gate:
            # The child epic has a gate that the final epic does not inherit.
            entry.write_text(entry.read_text().split('\n[[entry]]\nid = 2', 1)[0])
            epic = self.repo / "initiative/tickets/epic-test/epic-test.md"
            epic.write_text(epic.read_text().replace("parent: initiative", "parent: initiative\nafter: [epic-gate]"))
            breakdown = self.repo / "initiative/tickets/tickets.toml"
            breakdown.write_text(breakdown.read_text() + '\n[[epic]]\nid = 2\nslug = "epic-final"\ntitle = "Final"\n'
                                 '\n[[epic]]\nid = 3\nslug = "epic-gate"\ntitle = "Gate"\n')
            for name in ("final", "gate"):
                folder = self.repo / f"initiative/tickets/epic-{name}"
                (folder / "tickets").mkdir(parents=True)
                (folder / f"epic-{name}.md").write_text(f"---\ntype: epic\ntitle: {name}\nparent: initiative\n---\n")
                (folder / "tickets/tickets.toml").write_text(
                    '[[entry]]\nid = 1\ntype = "story"\ntitle = "Final"\n'
                    'description = "Given accepted children, final delivery completes."\nafter = ["1.1"]\n'
                    if name == "final" else "")
        d.git(self.repo, "add", ".")
        d.git(self.repo, "commit", "-m", "plan final integration delivery")
        d.git(self.repo, "push", "origin", "main", "main:integration")
        if base_gate:
            d.git(self.repo, "checkout", "-b", "integration-gate")
            gate = self.repo / "initiative/tickets/epic-gate/epic-gate.md"
            gate.write_text(gate.read_text().replace("type: epic", "type: epic\nstatus: done"))
            d.git(self.repo, "add", ".")
            d.git(self.repo, "commit", "-m", "controlled integration-only prerequisite acceptance")
            d.git(self.repo, "push", "origin", "HEAD:integration")
        d.git(self.repo, "checkout", "story")
        d.git(self.repo, "merge", "integration-gate" if base_gate else "main", "--no-edit")
        if child_fields or child_crlf:
            text = child_plan.read_text().replace("status: built", "status: built\n" + child_fields)
            child_plan.write_bytes((text.replace("\n", "\r\n") if child_crlf else text).encode())
            d.git(self.repo, "add", ".")
            d.git(self.repo, "commit", "-m", "retain clear native blocked fields")
        self.state["tasks"]["1.1"]["commit_sha"] = d.git(self.repo, "rev-parse", "HEAD")
        d.atomic(self.run_dir / "state.json", self.state)
        child_cfg = dict(self.cfg, base="integration")
        child_id = d.enqueue(child_cfg, self.repo, self.run_dir, "1.1")
        child_dir = Path(self.cfg["state_dir"]) / child_id
        child = d.read(child_dir / "job.json")
        if legacy_child:
            child["source_branch"] = ""
            d.atomic(child_dir / "job.json", child)
        def evidence_for(job, criterion):
            contract_path = str(Path(job["plan"]).parent / "tickets.toml")
            artifacts = [dict(path=p, sha256=hashlib.sha256((self.repo / p).read_bytes()).hexdigest())
                         for p in ("review.md", contract_path, job["plan"])]
            evidence = {k: job[k] for k in ("repository", "run", "ticket", "sha", "base", "container")}
            evidence.update(orchestrator="authorized", operator_gates=[], hitl=False, artifacts=artifacts,
                            review=dict(status="completed", sha=job["sha"], artifact="review.md"),
                            contract=dict(path=contract_path),
                            criteria=[dict(criterion=criterion, status="passed", artifact="review.md")])
            return evidence
        d.atomic(child_dir / "evidence.json", evidence_for(child, "Given a job, delivery completes."))
        child_worker = d.Worker(child_cfg)
        prs, _ = self.transport(child_worker, child, squash=True)
        if transient:
            native = child_worker.native
            child_worker.native = lambda *a: (_ for _ in ()).throw(d.Pending("controlled transient check"))
            self.assertFalse(child_worker.process(child_dir / "job.json"))
            child = d.read(child_dir / "job.json")
            self.assertEqual(child["status"], "waiting")
            child_worker.native = native
        child_worker.advance(child_dir, child)
        child = d.read(child_dir / "job.json")
        self.assertEqual(child["stage"], "complete")
        self.assertNotEqual(child["sha"], child["code_pr"]["merge_commit_sha"])
        d.git(self.repo, "fetch", "origin", "integration")
        d.git(self.repo, "checkout", "-b", "integration", "FETCH_HEAD")
        if multiple:
            second_plan = self.repo / "initiative/tickets/epic-test/tickets/story-second-plan.md"
            second_plan.write_text("---\ntitle: Second\nticket: 3\nstatus: built\noperator_actions: []\n---\n")
            d.git(self.repo, "add", ".")
            d.git(self.repo, "commit", "-m", "build second integration child")
            self.state["tasks"]["1.3"] = dict(phase="done", operator_actions=[],
                commit_sha=d.git(self.repo, "rev-parse", "HEAD"),
                ticket=dict(ref="1.3", plan=str(second_plan), title="Second"))
            d.atomic(self.run_dir / "state.json", self.state)
            second_id = d.enqueue(child_cfg, self.repo, self.run_dir, "1.3")
            second_dir = Path(self.cfg["state_dir"]) / second_id
            second = d.read(second_dir / "job.json")
            d.atomic(second_dir / "evidence.json", evidence_for(second, "Accept the second child."))
            second_worker = d.Worker(child_cfg)
            self.transport(second_worker, second, prs=prs, squash=True)
            second_worker.advance(second_dir, second)
            d.git(self.repo, "fetch", "origin", "integration")
            d.git(self.repo, "checkout", "--detach", "FETCH_HEAD")
        plan = self.repo / f"initiative/tickets/epic-{'final' if base_gate else 'test'}/tickets/story-final-plan.md"
        plan.write_text(f"---\ntitle: Final\nticket: {1 if base_gate else 2}\nstatus: built\noperator_actions: []\n---\n")
        # Unrelated entry-only work changes the shared file after child acceptance.
        entry.write_text(entry.read_text() + '\n[[entry]]\nid = 5\ntype = "story"\ntitle = "Later"\n')
        d.git(self.repo, "add", ".")
        d.git(self.repo, "commit", "-m", "build final delivery and plan unrelated work")
        final_ref = "2.1" if base_gate else "1.2"
        self.state["tasks"][final_ref] = dict(phase="done", branch="" if inplace else "integration", target_branch="main", operator_actions=[],
            commit_sha=d.git(self.repo, "rev-parse", "HEAD"),
            ticket=dict(ref=final_ref, plan=str(plan), title="Final"))
        d.atomic(self.run_dir / "state.json", self.state)
        final_id = d.enqueue(self.cfg, self.repo, self.run_dir, final_ref)
        directory = Path(self.cfg["state_dir"]) / final_id
        job = d.read(directory / "job.json")
        d.atomic(directory / "evidence.json", evidence_for(job, "Given accepted children, final delivery completes."))
        worker = d.Worker(self.cfg)
        prs, counts = self.transport(worker, job, prs=prs, squash=True)
        worker.initialize(directory, job)
        return directory, job, worker, child, prs, counts

    def test_multiple_accepted_children_after_transient_retry(self):
        directory, job, worker, child, prs, counts = self.integration_fixture(multiple=True, transient=True, legacy_child=True)
        checkout = directory / "checkout"
        audit_path = str(Path(child["plan"]).parent / f"delivery-{child['id']}.json")
        audit = json.loads(d.git(checkout, "show", f"{job['sha']}:{audit_path}"))
        self.assertEqual(audit["job"]["status"], "waiting")
        self.assertEqual(audit["job"]["source_branch"], "")
        worker.advance(directory, job)
        self.assertEqual(d.read(directory / "job.json")["stage"], "complete")
        self.assertEqual(len(prs), 6)
        self.assertEqual(counts, dict(code=1, tracking=1))

    def test_child_real_mark_clears_only_supported_blocked_fields(self):
        for fields, crlf in (('blocked_at: ""\nblocked_reason: ""', False),
                             ('blocked_at: "2020-01-01"\nblocked_reason: \'\'', False),
                             ('blocked_at: ""\nblocked_reason: ""', True)):
            with self.subTest(fields=fields, crlf=crlf):
                case = DeliveryTest()
                case.setUp()
                try:
                    directory, job, worker, child, _, _ = case.integration_fixture(child_fields=fields, child_crlf=crlf)
                    checkout = directory / "checkout"
                    audited = d.git(checkout, "show", f"{child['sha']}:{child['plan']}")
                    accepted = d.git(checkout, "show", f"{job['sha']}:{child['plan']}")
                    self.assertIn(fields, audited.replace("\r\n", "\n"))
                    if crlf:
                        self.assertIn("\r\n", audited)
                        self.assertIn("\r\n", accepted)
                    self.assertNotIn("blocked_reason:", accepted)
                    self.assertNotIn("blocked_at:", accepted)
                    worker.current_ticket(directory, job, job["sha"])
                finally:
                    case.doCleanups()

    def test_base_child_plan_changes_do_not_overwrite_new_work(self):
        directory, job, worker, child, _, counts = self.integration_fixture(child_baseline=True)
        checkout = directory / "checkout"
        # An unchanged older draft remains a supported baseline.
        worker.current_ticket(directory, job, job["sha"])
        original_main = d.git(self.repo, "rev-parse", "main")
        original = d.git(self.repo, "show", f"{original_main}:{child['plan']}") + "\n"
        for changed in (None, original.replace("status: draft", "status: in-progress"),
                        original.replace("status: draft", "status: in-review"),
                        original + "\n## Tasks & Acceptance\n- Complete new verification.\n",
                        original.replace("status: draft", 'status: built\nblocked_at: "2030-01-01"'),
                        original.replace("status: draft", "status: built") + "\n## Verification\nNew review evidence.\n"):
            with self.subTest(changed=changed):
                d.git(self.repo, "checkout", "--detach", original_main)
                plan = self.repo / child["plan"]
                if changed is None:
                    plan.unlink()
                else:
                    plan.write_text(changed)
                d.git(self.repo, "add", ".")
                d.git(self.repo, "commit", "-m", "new main-only child plan state")
                d.git(self.repo, "push", "--force", "origin", "HEAD:main")
                before = d.git(self.remote, "show-ref")
                with self.assertRaisesRegex(d.Pending, "child native plan"):
                    worker.current_ticket(directory, job, job["sha"])
                self.assertEqual(d.git(self.remote, "show-ref"), before)
                self.assertEqual(counts, dict(code=0, tracking=0))

    def test_met_main_prerequisite_does_not_need_integration_receipts(self):
        directory, job, worker, child, _, _ = self.integration_fixture()
        source_entry = (self.repo / "initiative/tickets/epic-test/tickets/tickets.toml").read_text()
        d.git(self.repo, "checkout", "main")
        entry = self.repo / "initiative/tickets/epic-test/tickets/tickets.toml"
        entry.write_text(entry.read_text().replace('[[entry]]\nid = 2',
            '[[entry]]\nid = 6\ntype = "story"\ntitle = "Main"\n\n[[entry]]\nid = 2')
            .replace('after = ["1.1"]', 'after = ["1.1", "1.6"]'))
        plan = entry.parent / "story-main-plan.md"
        plan.write_text("---\ntitle: Main\nticket: 6\nstatus: done\n---\n")
        d.atomic(entry.parent / ("delivery-" + "f" * 24 + ".json"),
                 dict(job=dict(ticket="1.6", container="initiative", base="main")))
        d.git(self.repo, "add", ".")
        d.git(self.repo, "commit", "-m", "ordinary main-accepted prerequisite")
        d.git(self.repo, "push", "origin", "main")
        d.git(self.repo, "checkout", "--detach", job["sha"])
        try:
            d.git(self.repo, "merge", "main", "--no-edit")
        except RuntimeError as error:
            self.assertIn("CONFLICT", str(error))
            entry.write_text(source_entry.replace('[[entry]]\nid = 2',
                '[[entry]]\nid = 6\ntype = "story"\ntitle = "Main"\n\n[[entry]]\nid = 2')
                .replace('after = ["1.1"]', 'after = ["1.1", "1.6"]'))
            d.git(self.repo, "add", str(entry))
            d.git(self.repo, "commit", "--no-edit")
        job["sha"] = d.git(self.repo, "rev-parse", "HEAD")
        d.git(directory / "checkout", "fetch", str(self.repo), job["sha"])
        with patch.object(worker, "pr", wraps=worker.pr) as pr:
            worker.current_ticket(directory, job, job["sha"])
            self.assertTrue(pr.call_args_list)
            self.assertTrue(all(call.args[0]["ticket"] == child["ticket"] for call in pr.call_args_list))

    def test_accepted_child_does_not_waive_its_base_gated_by(self):
        directory, job, worker, child, prs, counts = self.integration_fixture(base_gate=True)
        before = d.git(self.remote, "show-ref")
        with self.assertRaisesRegex(d.Pending, "prerequisite gates"):
            worker.current_ticket(directory, job, job["sha"])
        self.assertEqual(d.git(self.remote, "show-ref"), before)
        self.assertEqual(counts, dict(code=0, tracking=0))

    def test_after_proof_cannot_waive_epic_gated_by_or_ambiguous_dependencies(self):
        worker = d.Worker(self.cfg)
        row = dict(ref="1.1", epic="epic-child", id=1, file="", state="planned")
        status = dict(tickets=[row], epics=[dict(slug="epic-required", status="planned")])
        for after, gated_by, rows in ((["epic-required"], [], [row]), ([], ["1.1"], [row]),
                                      (["1.1"], [], [row, row])):
            with self.subTest(after=after, gated_by=gated_by, count=len(rows)):
                status["tickets"] = rows
                with patch.object(d, "run", return_value=json.dumps(status)), patch.object(worker, "accepted_child") as proof:
                    with self.assertRaisesRegex(d.Pending, "prerequisite gates"):
                        worker.prerequisites(self.repo, dict(container="initiative"),
                                             dict(epic="epic-final", after=after, gated_by=gated_by), proof)
                    proof.assert_not_called()

    def test_child_proof_cycle_is_pending_before_network_or_mutations(self):
        directory, job = self.queued()
        worker = d.Worker(self.cfg)
        before = d.read(directory / "job.json")
        with patch.object(worker, "api") as api, patch.object(d, "git") as git:
            with self.assertRaisesRegex(d.Pending, "proof cycle"):
                worker.accepted_child(directory, job, dict(ref="1.1"), job["sha"], {"1.1"})
            api.assert_not_called()
            git.assert_not_called()
        self.assertEqual(d.read(directory / "job.json"), before)

    def test_accepted_integration_child_final_delivery_and_restart(self):
        directory, job, worker, child, prs, counts = self.integration_fixture(inplace=True)
        self.assertEqual(job["source_branch"], "integration")
        self.assertEqual(d.read(self.run_dir / "state.json")["tasks"]["1.2"]["branch"], "")
        checkout = directory / "checkout"
        base = worker.current_ticket(directory, job, job["sha"])
        self.assertEqual(base, d.git(self.remote, "rev-parse", "main"))
        # Proof fetches leave FETCH_HEAD at a child receipt, never at main.
        self.assertNotEqual(base, d.git(checkout, "rev-parse", "FETCH_HEAD"))
        self.assertEqual(d.git(checkout, "rev-parse", "HEAD"), job["sha"])
        original = worker.gh
        interrupted = []
        def gh(*args):
            result = original(*args)
            if args[:2] == ("pr", "merge") and not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt("restart after final code merge")
            return result
        worker.gh = gh
        with self.assertRaises(KeyboardInterrupt):
            worker.advance(directory, job)
        worker.gh = original
        worker.advance(directory, d.read(directory / "job.json"))
        final = d.read(directory / "job.json")
        self.assertEqual(final["stage"], "complete")
        self.assertEqual(counts, dict(code=1, tracking=1))
        self.assertEqual(len(prs), 4)
        self.assertEqual(final["tracking_base"], final["code_pr"]["merge_commit_sha"])
        self.assertIn("status: done", d.git(checkout, "show", f"{final['tracking_sha']}:{job['plan']}"))
        self.assertEqual(d.git(self.repo, "status", "--porcelain"), "")

    def test_integration_proof_failure_matrix_before_final_push(self):
        import copy
        directory, original_job, worker, child, prs, counts = self.integration_fixture()
        audit_path = str(Path(child["plan"]).parent / f"delivery-{child['id']}.json")
        original_audit = json.loads((self.repo / audit_path).read_text())
        original_prs = copy.deepcopy(prs)
        pinned = original_job["sha"]
        d.git(self.repo, "fetch", "origin", prs[2]["head"]["sha"])
        d.git(self.repo, "checkout", "--detach", "FETCH_HEAD")
        (self.repo / "source").write_text("unrelated tracking change")
        d.git(self.repo, "add", ".")
        d.git(self.repo, "commit", "-m", "controlled invalid tracking footprint")
        bad_tracking_head = d.git(self.repo, "rev-parse", "HEAD")
        d.git(self.repo, "push", "origin", "HEAD:refs/heads/controlled-invalid-tracking-head")
        cases = ("missing", "duplicate", "id", "repository", "ticket", "container", "plan", "base", "branch",
                 "review", "artifact", "native", "check", "check-head", "workflow", "workflow-head", "status",
                 "expected-check", "code-head", "code-merge", "tracking-missing",
                 "tracking-duplicate", "tracking-base", "tracking-repo", "tracking-head", "tracking-merge", "tracking-footprint",
                 "tracking-unmerged", "plan-changed", "audit-changed", "review-only", "human", "contract",
                 "source-branch-empty", "source-branch-base")
        for case in cases:
            with self.subTest(case=case):
                d.git(self.repo, "checkout", "--detach", pinned)
                audit = copy.deepcopy(original_audit)
                job = copy.deepcopy(original_job)
                prs.clear()
                prs.update(copy.deepcopy(original_prs))
                path = self.repo / audit_path
                if case == "missing":
                    path.unlink()
                elif case == "duplicate":
                    duplicate = path.with_name("delivery-" + "f" * 24 + ".json")
                    duplicate.write_text(json.dumps(audit))
                elif case in ("id", "repository", "ticket", "container", "plan", "base"):
                    audit["job"][case] = "foreign"
                elif case == "branch":
                    audit["tracking_branch"] = "foreign"
                elif case == "review":
                    audit["evidence"]["review"]["sha"] = "0" * 40
                elif case == "audit-changed":
                    audit["tracking_audit"] = "changed after acceptance"
                elif case == "artifact":
                    audit["evidence"]["artifacts"][0]["sha256"] = "0" * 64
                elif case == "native":
                    audit["job"]["code_verification"]["local"]["results"][1]["status"] = "failed"
                elif case in ("check", "check-head", "workflow", "workflow-head", "status", "expected-check"):
                    github = audit["job"]["code_verification"]["github"]
                    if case == "status":
                        github["statuses"] = [dict(context="ci", state="failure")]
                    elif case == "expected-check":
                        github["actions"] = "enabled"
                    else:
                        github["checks" if case.startswith("check") else "workflows"] = [dict(
                            name="ci", head_sha="0" * 40 if case.endswith("head") else child["sha"],
                            status="completed", conclusion="success" if case.endswith("head") else "cancelled")]
                elif case == "code-head":
                    prs[1]["head"]["sha"] = pinned
                elif case == "code-merge":
                    prs[1]["merge_commit_sha"] = pinned
                elif case == "tracking-missing":
                    del prs[2]
                elif case == "tracking-duplicate":
                    prs[3] = copy.deepcopy(prs[2])
                elif case == "tracking-base":
                    prs[2]["base"]["ref"] = "main"
                elif case == "tracking-repo":
                    prs[2]["head"]["repo"]["full_name"] = "foreign/repo"
                elif case == "tracking-head":
                    prs[2]["head"]["ref"] = "foreign"
                elif case == "tracking-merge":
                    prs[2]["merge_commit_sha"] = pinned
                elif case == "tracking-footprint":
                    prs[2]["head"]["sha"] = bad_tracking_head
                elif case == "tracking-unmerged":
                    prs[2]["merged"] = False
                elif case in ("plan-changed", "review-only", "human"):
                    plan = self.repo / child["plan"]
                    plan.write_text(plan.read_text().replace("status: done", "status: built") if case == "review-only"
                                    else plan.read_text() + ("\nChanged acceptance.\n" if case == "plan-changed" else ""))
                    if case == "human":
                        plan.write_text(plan.read_text().replace("operator_actions: []", "operator_actions: [approve]"))
                elif case == "contract":
                    contract = self.repo / "initiative/tickets/epic-test/tickets/tickets.toml"
                    contract.write_text(contract.read_text().replace("Given a job, delivery completes.", "Changed child contract."))
                elif case == "source-branch-empty":
                    job["source_branch"] = ""
                elif case == "source-branch-base":
                    job["source_branch"] = "main"
                if case != "missing":
                    path.write_text(json.dumps(audit, indent=2))
                d.git(self.repo, "add", ".")
                d.git(self.repo, "commit", "--allow-empty", "-m", f"controlled invalid proof {case}")
                job["sha"] = d.git(self.repo, "rev-parse", "HEAD")
                d.git(directory / "checkout", "fetch", str(self.repo), job["sha"])
                before = d.git(self.remote, "show-ref")
                with self.assertRaises(d.Pending):
                    worker.current_ticket(directory, job, job["sha"])
                self.assertEqual(d.git(self.remote, "show-ref"), before)
                self.assertEqual(counts, dict(code=0, tracking=0))
        d.git(self.repo, "checkout", "--detach", pinned)

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
                    case.state["tasks"]["1.1"].update(commit_sha=case.sha, branch="isolated-story")
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
