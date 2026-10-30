"""Durable delivery worker. Python standard library; no loop core changes."""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlsplit

try:
    from bmad_loop.plugins.model import Plugin, PluginError
except ImportError:  # The standalone CLI does not require the loop installation.
    class Plugin:
        def __init__(self, manifest, settings):
            self.manifest, self.settings = manifest, settings
    PluginError = ValueError


class Pending(Exception):
    pass


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read(path):
    return json.loads(Path(path).read_text())


# Git's `rev-parse --local-env-vars`: hooks export these for their own repository.
GIT_LOCAL_ENV = frozenset({
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
    "GIT_SHALLOW_FILE", "GIT_COMMON_DIR",
})


def run(args, cwd=None, timeout=120, env=None, strip=True, stdin=None):
    env = {key: value for key, value in (os.environ if env is None else env).items()
           if key not in GIT_LOCAL_ENV}
    # Kill the process group on timeout, including Git hooks and their children.
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(args, cwd=cwd, env=env, stdin=stdin, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
        output.seek(0)
        result = output.read().decode(errors="replace")
        if code:
            raise RuntimeError(f"{args[0]} exited {code}: {result[-3000:]}")
        return result.strip() if strip else result


def git(root, *args):
    return run(["git", *args], root)


def github_origin(origin, repository, checkout=None):
    """Verify API identity separately from the unchanged Git SSH transport."""
    ssh_args = None
    if "://" in origin:
        url = urlsplit(origin)
        if (url.scheme not in ("https", "ssh") or not url.hostname or url.query
                or url.fragment or url.password or (url.scheme == "https" and url.username)):
            raise Pending("unsupported GitHub origin")
        host, path = url.hostname, url.path.lstrip("/")
        if url.scheme == "ssh":
            ssh_args = ["ssh", "-G"]
            if url.port:
                ssh_args += ["-p", str(url.port)]
            target = f"{url.username}@{host}" if url.username else host
        elif url.port:
            host += f":{url.port}"
    else:
        match = re.fullmatch(r"(?:(?P<user>[\w.-]+)@)?(?P<host>[\w.-]+):(?P<path>[^:]+)", origin)
        if not match:
            raise Pending("unsupported GitHub origin")
        host, path = match["host"], match["path"]
        target = f"{match['user']}@{host}" if match["user"] else host
        ssh_args = ["ssh", "-G"]
    path = path.removesuffix(".git")
    if path != repository:
        raise Pending("origin does not match authorized repository")
    if ssh_args is not None:
        # Custom commands can select a different SSH configuration or transport.
        if (os.environ.get("GIT_SSH_COMMAND") or os.environ.get("GIT_SSH")
                or os.environ.get("GIT_SSH_VARIANT", "ssh") not in ("ssh", "auto")
                or git(checkout, "config", "--get", "--default", "", "core.sshCommand")):
            raise Pending("custom Git SSH command/variant requires separate transport verification")
        if not re.fullmatch(r"[\w.-]+(?:@[\w.-]+)?", target) or target.startswith("-"):
            raise Pending("unsupported SSH origin")
        resolved = [line.split()[1] for line in run([*ssh_args, target]).splitlines()
                    if line.startswith("hostname ") and len(line.split()) == 2]
        if len(resolved) != 1:
            raise Pending("SSH origin hostname unresolved")
        host = resolved[0]
    if host.lower() != os.environ.get("GH_HOST", "github.com").lower():
        raise Pending("origin host does not match GitHub API host")
    return f"https://{host}/{path}"


@contextlib.contextmanager
def lock(path, blocking=False):
    Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def push_policy(cfg):
    policy = cfg.get("push_policy", "hook-required")
    if policy not in ("hook-required", "native-checks-only"):
        raise ValueError("push_policy must be hook-required or native-checks-only")
    return policy


def check_native(receipt, sha):
    results = receipt.get("results") if isinstance(receipt, dict) else None
    commands = [["pnpm", "install", "--frozen-lockfile"], ["pnpm", "lint"],
                ["pnpm", "typecheck"], ["pnpm", "test"]]
    if (not isinstance(results, list) or receipt.get("sha") != sha or receipt.get("platform") != "darwin"
            or not all(isinstance(row, dict) for row in results)
            or [row.get("command") for row in results] != commands
            or any(row.get("status") != "passed" for row in results)):
        raise Pending("complete successful native checks for the exact source SHA are required")


def check_source(checkout, sha):
    if git(checkout, "rev-parse", "HEAD") != sha or git(checkout, "status", "--porcelain"):
        raise Pending("delivery requires a clean pinned checkout")


def config(path):
    path = Path(path).expanduser().resolve()
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o022:
        raise ValueError("configuration must be owned by this user and not group/world writable")
    value = read(path)
    push_policy(value)
    for field in ("repository", "base", "state_dir", "orchestrators", "expected_checks"):
        if field not in value:
            raise ValueError(f"missing setting: {field}")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", value["repository"]):
        raise ValueError("repository must be owner/name")
    if not value["orchestrators"] or not isinstance(value["expected_checks"], list):
        raise ValueError("orchestrators and expected_checks must be explicit")
    value["state_dir"] = str(Path(value["state_dir"]).expanduser().resolve())
    return value


def relative(root, path):
    return str(Path(path).resolve().relative_to(Path(root).resolve()))


def pin_source(root, source_ref, sha, job_id):
    env = {key: value for key, value in os.environ.items() if key not in GIT_LOCAL_ENV}
    with tempfile.TemporaryFile() as errors, concurrent.futures.ThreadPoolExecutor(max_workers=1) as reader:
        process = subprocess.Popen(["git", "update-ref", "--stdin"], cwd=root, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
            text=True, start_new_session=True)
        commit_timeout = None
        commit_acknowledged = False
        try:
            process.stdin.write(f"start\nverify HEAD {sha}\nupdate refs/delivery/{job_id} {sha}\nprepare\n")
            process.stdin.flush()
            for expected in ("start: ok\n", "prepare: ok\n"):
                if reader.submit(process.stdout.readline).result(timeout=120) != expected:
                    errors.seek(0)
                    raise RuntimeError("source pin transaction refused: " + errors.read().decode(errors="replace")[-3000:])
            # Prepared verify HEAD holds both HEAD and its referent until commit or abort.
            try:
                attached = git(root, "symbolic-ref", "--no-recurse", "--quiet", "HEAD")
            except RuntimeError as error:
                raise Pending("source branch requires an attached HEAD") from error
            if attached != source_ref:
                raise Pending("source branch changed before pin transaction")
            try:
                output, _ = process.communicate("commit\n", timeout=120)
            except subprocess.TimeoutExpired as error:
                commit_timeout = error
            else:
                if process.returncode or output != "commit: ok\n":
                    errors.seek(0)
                    raise RuntimeError("source pin commit refused: " + errors.read().decode(errors="replace")[-3000:])
                commit_acknowledged = True
        finally:
            if process.poll() is None:
                if not process.stdin.closed:
                    try:
                        process.stdin.write("abort\n")
                        process.stdin.flush()
                    except BrokenPipeError:
                        pass
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
            if not commit_acknowledged:
                # Descendants can outlive a graceful abort or an already-exited Git leader.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            with contextlib.suppress(BrokenPipeError):
                process.stdin.close()
            process.stdout.close()
        if commit_timeout:
            # A committed hook can time out after the verified pin is already durable.
            try:
                pinned = git(root, "rev-parse", "--verify", f"refs/delivery/{job_id}")
            except RuntimeError:
                pinned = None
            if pinned != sha:
                raise Pending("source pin commit outcome unconfirmed; preserve pin and Git locks for inspection") from commit_timeout


def enqueue(cfg, root, run_dir, key, sha=None, phase=None):
    state = read(Path(run_dir) / "state.json")
    task = state["tasks"][key]
    ticket = task.get("ticket")
    if (phase or task.get("phase")) != "done" or not ticket:
        return None
    if task.get("operator_actions") or ticket.get("hitl") or ticket.get("done_checkpoint"):
        raise Pending("ticket has operator/HITL/checkpoint gates")
    sha = sha or task.get("commit_sha")
    if not sha or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("missing pinned commit")
    git(root, "cat-file", "-e", sha + "^{commit}")
    binding_root = state.get("repo_root") or root
    container = relative(binding_root, state["initiative_folder"])
    plan = relative(binding_root, ticket["plan"])
    identity = dict(repository=cfg["repository"], run=str(Path(run_dir).resolve()),
                    ticket=ticket["ref"], sha=sha, base=cfg["base"], container=container)
    job_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    directory = Path(cfg["state_dir"]) / job_id
    with lock(Path(cfg["state_dir"]) / "enqueue.lock", blocking=True):
        if not (directory / "job.json").exists():
            source_branch = task.get("branch", "")
            source_gitdir = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
            if not source_branch:
                if Path(source_gitdir).resolve() != Path(git(binding_root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve():
                    raise Pending("source checkout does not belong to the run repository")
                try:
                    source_ref = git(root, "symbolic-ref", "--quiet", "HEAD")
                except RuntimeError as error:
                    raise Pending("source branch requires an attached HEAD") from error
                if (not source_ref.startswith("refs/heads/")
                        or git(root, "rev-parse", "HEAD") != sha
                        or git(root, "rev-parse", source_ref) != sha
                        or git(root, "symbolic-ref", "--quiet", "HEAD") != source_ref):
                    raise Pending("source branch does not match pinned commit")
                source_branch = source_ref.removeprefix("refs/heads/")
                pin_source(root, source_ref, sha, job_id)
            else:
                git(root, "update-ref", f"refs/delivery/{job_id}", sha)
            settings = {}
            for field in ("user.name", "user.email", "core.hooksPath"):
                try:
                    value = git(root, "config", "--get", field)
                except RuntimeError:
                    if field == "core.hooksPath":
                        settings[field] = str(Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "hooks")
                        continue
                    raise
                settings[field] = value
            atomic(directory / "job.json", dict(identity, id=job_id, plan=plan,
                   title=ticket["title"], stage="code", attempts=0, next_try=0,
                   status="queued", source_branch=source_branch, git_settings=settings,
                   origin=git(root, "remote", "get-url", "origin"),
                   source_gitdir=source_gitdir))
    return job_id


class DeliveryPlugin(Plugin):
    def validate(self, policy):
        if not self.settings.get("config"):
            return
        if getattr(getattr(policy, "tickets", None), "mark_done", None) != "never":
            raise PluginError('delivery requires tickets.mark_done = "never"')

    def capture(self, ctx):
        if not self.settings.get("config"):
            return
        try:
            cfg = config(self.settings["config"])
            if ctx.stage == "post_commit":
                if ctx.phase != "done":
                    return
                root = ctx.worktree or ctx.repo_root
                enqueue(cfg, root, ctx.run_dir, ctx.story_key,
                        git(root, "rev-parse", "HEAD"), ctx.phase)
            else:
                reconcile(cfg, ctx.repo_root, ctx.run_dir)
            # Run the granted snapshot, never the agent-writable live plugin.
            subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()),
                              "--config", self.settings["config"], "worker"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except Exception as error:
            ctx.shared["delivery_error"] = f"{type(error).__name__}: {error}"

    on_post_commit = capture
    on_post_story = capture
    on_pre_run = capture
    on_post_run = capture


def reconcile(cfg, root, run_dir):
    """Journal closes the post_commit-before-state-save crash window."""
    state = read(Path(run_dir) / "state.json")
    events = {}
    journal = Path(run_dir) / "journal.jsonl"
    if journal.exists():
        for line in journal.read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("kind") in ("story-done", "story-awaiting-operator"):
                events[event.get("story_key")] = event
    errors = {}
    for key, task in state.get("tasks", {}).items():
        event = events.get(key, {})
        if event.get("kind") == "story-awaiting-operator":
            continue
        sha = event.get("commit") or task.get("commit_sha")
        phase = "done" if event.get("kind") == "story-done" else task.get("phase")
        if phase == "done":
            try:
                enqueue(cfg, root, run_dir, key, sha, phase)
            except Exception as error:
                errors[key] = str(error)
    atomic(Path(cfg["state_dir"]) / "reconcile-errors.json", errors)


def obligations(text, sections=True, fallback=True):
    """Keep prose and list continuations; never discard unnumbered obligations."""
    bodies = []
    if sections:
        headings = list(re.finditer(r"(?m)^(#{1,6}) +(.+?)\s*$", text))
        end = 0
        for i, heading in enumerate(headings):
            if heading.start() < end or heading[2].strip().casefold() not in {
                    "description", "acceptance criteria", "local acceptance criteria", "requirements", "tasks & acceptance"}:
                continue
            end = next((h.start() for h in headings[i + 1:] if len(h[1]) <= len(heading[1])), len(text))
            bodies.append(text[heading.end():end].strip())
    if not bodies and fallback:
        bodies = [text.strip()]
    result = []
    for body in bodies:
        for item in re.split(r"(?m)^(?=\d+\. |[-*] )|\n\s*\n", body):
            item = re.sub(r"^(?:\d+\. |[-*] )(?:\[[ xX]\] )?", "", item.strip()).strip()
            if item and not re.fullmatch(r"\*\*[^*\n]+:\*\*", item):
                result.append(item)
    return result


def check_gates(text):
    """Read gate fields only from frontmatter; unknown nonempty values fail closed."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise Pending("missing plan/story frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise Pending("malformed plan/story frontmatter")
    fields = {}
    key = None
    for line in lines[1:end]:
        match = re.match(r"^([\w-]+):\s*(.*)$", line)
        if match:
            key = match[1]
            if key in fields:
                raise Pending("duplicate frontmatter field")
            fields[key] = re.sub(r"\s+#.*$", "", match[2]).strip()
        elif line.strip() and not line.lstrip().startswith("#") and key:
            fields[key] += "\n" + line.strip()
    for key in ("operator_actions", "blocked_reason"):
        value = fields.get(key, "").strip()
        if not re.fullmatch(r"\[\s*\]", value) and value not in ("", "null", "~", '\"\"', "''"):
            raise Pending(f"current plan has unresolved gates: {key}")
    for key in ("hitl", "done_checkpoint"):
        if fields.get(key, "false").lower() not in ("false", "", "null", "~"):
            raise Pending(f"current plan has unresolved gates: {key}")
    if fields.get("status", "").strip("\"'") in ("awaiting-operator", "blocked", "dropped"):
        raise Pending("current plan has unresolved status gate")


def accepted_plan(text):
    """Apply only the supported Ticket Tree mark-done frontmatter edits."""
    check_gates(text)
    crlf = "\r\n" in text
    text = text.replace("\r\n", "\n")
    match = re.match(r"\A---\n(.*?)\n---(?:\n|\Z)", text, re.S)
    if not match:
        raise Pending("malformed native plan")
    block = match[1]
    for key, value in (("status", "done"), ("blocked_at", ""), ("blocked_reason", "")):
        pattern = re.compile(rf"^{key}:.*$\n?", re.M)
        if not value:
            block = pattern.sub("", block).rstrip("\n")
        elif pattern.search(block):
            block = pattern.sub(lambda _: f"{key}: {value}\n", block, count=1).rstrip("\n")
        else:
            block += f"\n{key}: {value}"
    result = text[:match.start(1)] + block + text[match.end(1):]
    return result.replace("\n", "\r\n") if crlf else result


class Worker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.push_policy = push_policy(cfg)

    def gh(self, *args):
        return run(["gh", *args], timeout=self.cfg.get("command_timeout", 120))

    def api(self, endpoint):
        return json.loads(self.gh("api", endpoint))

    def pages(self, endpoint, key=None):
        result = []
        for page in range(1, 1001):
            data = self.api(endpoint + ("&" if "?" in endpoint else "?") + f"per_page=100&page={page}")
            rows = data[key] if key else data
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise Pending("pagination limit exceeded")

    def save(self, directory, job):
        atomic(directory / "job.json", job)

    def evidence(self, directory, job, supplied=None):
        path = directory / "evidence.json"
        if supplied is None and not path.exists():
            raise Pending("missing orchestrator evidence")
        evidence = read(path) if supplied is None else supplied
        for key in ("repository", "run", "ticket", "sha", "base", "container"):
            if evidence.get(key) != job[key]:
                raise Pending(f"evidence identity mismatch: {key}")
        if evidence.get("orchestrator") not in self.cfg["orchestrators"]:
            raise Pending("unauthorized evidence orchestrator")
        if evidence.get("operator_gates") != [] or evidence.get("hitl") is not False:
            raise Pending("operator/HITL clearance missing")
        review = evidence.get("review", {})
        if review.get("status") != "completed" or review.get("sha") != job["sha"]:
            raise Pending("completed exact-head review missing")
        artifacts = evidence.get("artifacts", [])
        if not artifacts or review.get("artifact") not in [a.get("path") for a in artifacts]:
            raise Pending("review artifact missing")
        checkout = directory / "checkout"
        for artifact in artifacts:
            path = artifact["path"]
            if Path(path).is_absolute() or ".." in Path(path).parts:
                raise Pending("artifact path escapes source")
            content = run(["git", "show", f"{job['sha']}:{path}"], checkout, strip=False).encode()
            if hashlib.sha256(content).hexdigest() != artifact.get("sha256"):
                raise Pending(f"artifact hash mismatch: {path}")
        criteria = evidence.get("criteria", [])
        if not criteria or any(c.get("status") != "passed" or not c.get("criterion") or
                               c.get("artifact") not in [a["path"] for a in artifacts] for c in criteria):
            raise Pending("criterion-level evidence missing")
        found = self.ticket(checkout, job, job["sha"])
        contract = evidence.get("contract", {})
        contract_path = relative(checkout, found["story_file"]) if found.get("story_file") else str(Path(job["plan"]).parent / "tickets.toml")
        if contract.get("path") != contract_path:
            raise Pending("contract is not the bound ticket source")
        if contract_path not in [a["path"] for a in artifacts]:
            raise Pending("pinned acceptance contract missing")
        sources = [(contract_path, git(checkout, "show", f"{job['sha']}:{contract_path}")
                    if found.get("story_file") else found.get("description", ""))]
        required = obligations(sources[0][1])
        if found.get("verify", "").strip():
            required.extend(obligations(found["verify"], sections=False))
        plan_text = git(checkout, "show", f"{job['sha']}:{job['plan']}")
        required.extend(obligations(plan_text, fallback=False))
        if job["plan"] not in [a["path"] for a in artifacts]:
            raise Pending("pinned native plan artifact missing")
        if not required or sorted(set(required)) != sorted(c["criterion"] for c in criteria):
            raise Pending("evidence must cover every contract, verify and plan criterion exactly")
        return evidence

    def ticket(self, checkout, job, sha, after_proof=None):
        git(checkout, "checkout", "--detach", sha)
        runtime = checkout / "_bmad/custom"
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "config.user.toml").write_text('[core]\nticket_store = "repo"\n')
        exclude = checkout / ".git/info/exclude"
        if "_bmad/" not in exclude.read_text():
            with exclude.open("a") as stream:
                stream.write("\n_bmad/\n")
        script = self.cfg.get("tickets_script", ".agents/skills/bmad-agent-po/scripts/tickets.py")
        found = json.loads(run([sys.executable, script, "--project-root", str(checkout),
                               "find", job["container"], job["ticket"]], checkout))
        if relative(checkout, found["plan"]) != job["plan"] or found.get("ref") != job["ticket"]:
            raise Pending("ticket identity changed")
        if found.get("hitl") or found.get("done_checkpoint") or found.get("unknown") or found.get("drift") or found.get("blocked_reason") or found.get("status") in ("blocked", "dropped", "awaiting-operator"):
            raise Pending("current ticket has operator/HITL/status gates")
        plan = checkout / job["plan"]
        if plan.exists():
            check_gates(plan.read_text())
        if found.get("story_file"):
            check_gates(Path(found["story_file"]).read_text())
        self.prerequisites(checkout, job, found, after_proof)
        return found

    def prerequisites(self, checkout, job, found, after_proof=None):
        script = self.cfg.get("tickets_script", ".agents/skills/bmad-agent-po/scripts/tickets.py")
        status = json.loads(run([sys.executable, script, "--project-root", str(checkout),
                                 "status", job["container"]], checkout))
        rows, epics = status["tickets"], status.get("epics", [])
        def satisfied(dependency, review=False):
            matches = []
            for row in rows:
                aliases = {str(row["ref"]), f"{row['epic']}/{row['id']}", f"{row['epic']}/{row['file']}"}
                if row["epic"] == found["epic"]:
                    aliases.update((str(row["id"]), str(row["file"])))
                if str(dependency) in aliases:
                    matches.append((row["state"] in (("done", "review") if review else ("done",)), row))
            matches.extend((e.get("status") == "done", None) for e in epics if e["slug"] == dependency)
            if len(matches) != 1:
                return False
            satisfied, row = matches[0]
            if review and row is not None and after_proof:
                return after_proof(row, satisfied)
            return satisfied
        if (any(not satisfied(dep, review=True) for dep in found.get("after", [])) or
                any(not satisfied(dep) for dep in found.get("gated_by", []))):
            raise Pending("current ticket has unresolved prerequisite gates")

    def child_base_plan(self, checkout, child, base_sha, source_sha):
        def plan_at(revision):
            if not git(checkout, "ls-tree", revision, "--", child["plan"]):
                return None
            return run(["git", "show", f"{revision}:{child['plan']}"], checkout, strip=False)
        current = plan_at(base_sha)
        code_merge = child["code_pr"]["merge_commit_sha"]
        baseline = git(checkout, "merge-base", code_merge, base_sha)
        if current == plan_at(baseline):
            return
        if current is None:
            raise Pending("child native plan removed from base")
        status = re.search(r"(?m)^status: *[\"']?([\w-]+)", current.split("---", 2)[1])
        if not status or status[1] not in ("built", "done"):
            raise Pending("child native plan has a newer build/review state on base")
        check_gates(current)
        def comparable(text):
            frontmatter, body = text.split("---", 2)[1:]
            return re.sub(r"(?m)^status:.*$", "status: built", frontmatter) + "---" + body
        if comparable(current) not in (comparable(plan_at(source_sha)), comparable(plan_at(code_merge))):
            raise Pending("child native plan changed on base")

    def accepted_child(self, directory, job, row, base_sha, visiting=None, base_satisfied=False):
        """Allow only a directly accepted integration child absent from the final base."""
        checkout = directory / "checkout"
        visiting = set() if visiting is None else visiting
        key = str(row["ref"])
        if key in visiting:
            raise Pending("accepted child prerequisite proof cycle")
        visiting.add(key)
        try:
            branch = job.get("source_branch")
            if not branch or branch == job["base"]:
                raise Pending("accepted child needs a distinct integration source branch")
            paths = git(checkout, "ls-tree", "-r", "--name-only", job["sha"], "--", job["container"]).splitlines()
            candidates = []
            for path in paths:
                if not re.fullmatch(r"delivery-[0-9a-f]{24}\.json", Path(path).name):
                    continue
                audit = json.loads(git(checkout, "show", f"{job['sha']}:{path}"))
                child = audit.get("job", {})
                if base_satisfied and child.get("base") != branch:
                    continue
                if str(child.get("ticket")) == str(row["ref"]) and child.get("container") == job["container"]:
                    candidates.append((path, audit))
            if base_satisfied and not candidates:
                return True
            if len(candidates) != 1:
                raise Pending("accepted child needs exactly one tracked delivery audit")
            audit_path, audit = candidates[0]
            child = audit["job"]
            identity = {key: child[key] for key in ("repository", "run", "ticket", "sha", "base", "container")}
            expected_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
            plan = child["plan"]
            if (child.get("id") != expected_id or child["repository"] != job["repository"] or
                    child["base"] != branch or child["container"] != job["container"] or
                    child.get("stage") != "tracking" or
                    not isinstance(child["run"], str) or not Path(child["run"]).is_absolute() or
                    not re.fullmatch(r"[0-9a-f]{40}", child["sha"]) or
                    Path(plan).is_absolute() or ".." in Path(plan).parts or
                    not Path(plan).is_relative_to(job["container"]) or
                    audit_path != str(Path(plan).parent / f"delivery-{expected_id}.json") or
                    audit.get("tracking_branch") != f"delivery/{expected_id}-tracking" or
                    audit.get("code_pr") != child.get("code_pr")):
                raise Pending("accepted child audit identity mismatch")
            # Validate all recorded identities before any auxiliary fetch.
            recorded = audit["code_pr"]
            code_branch = f"delivery/{expected_id}-code"
            def receipt(full, sha, expected_branch):
                self.confirm_merge(child, sha, full)
                if (not full.get("merged_at") or full.get("state") != "closed" or
                        type(full.get("number")) is not int or full["number"] < 1 or
                        full.get("head", {}).get("ref") != expected_branch or
                        full.get("head", {}).get("repo", {}).get("full_name") != job["repository"]):
                    raise Pending("accepted child PR branch/repository mismatch")
            receipt(recorded, child["sha"], code_branch)
            self.child_base_plan(checkout, child, base_sha, job["sha"])
            if base_satisfied:
                return True
            verification = child["code_verification"]
            local, github = verification["local"], verification["github"]
            commands = [["pnpm", "install", "--frozen-lockfile"], ["pnpm", "lint"],
                        ["pnpm", "typecheck"], ["pnpm", "test"]]
            if (verification.get("sha") != child["sha"] or local.get("sha") != child["sha"] or
                    local.get("platform") != "darwin" or
                    [r.get("command") for r in local.get("results", [])] != commands or
                    any(r.get("status") != "passed" for r in local["results"]) or
                    github.get("actions") not in ("enabled", "disabled") or
                    not all(isinstance(github.get(key), list) for key in ("checks", "statuses", "workflows")) or
                    type(github.get("protection", {}).get("protected")) is not bool):
                raise Pending("accepted child exact-head checks missing")
            for check in github["checks"]:
                if (check.get("head_sha") != child["sha"] or check.get("status") != "completed" or
                        check.get("conclusion") not in ("success", "neutral", "skipped")):
                    raise Pending("accepted child recorded GitHub check failed or changed head")
            for workflow in github["workflows"]:
                if (workflow.get("head_sha") != child["sha"] or workflow.get("status") != "completed" or
                        workflow.get("conclusion") not in ("success", "neutral", "skipped")):
                    raise Pending("accepted child recorded workflow failed or changed head")
            if any(status.get("state") != "success" for status in github["statuses"]):
                raise Pending("accepted child recorded GitHub status failed")
            registered = {check["name"] for check in github["checks"]}
            registered.update(status["context"] for status in github["statuses"])
            if github["actions"] == "enabled" and (not self.cfg["expected_checks"] or
                    not set(self.cfg["expected_checks"]) <= registered):
                raise Pending("accepted child expected checks missing")
            code = self.pr(child, code_branch)
            tracking = self.pr(child, audit["tracking_branch"])
            if not code or not tracking or code.get("number") != recorded.get("number"):
                raise Pending("accepted child code/tracking PR missing")
            code_number, tracking_number = code["number"], tracking["number"]
            code = self.api(f"repos/{job['repository']}/pulls/{code_number}")
            tracking = self.api(f"repos/{job['repository']}/pulls/{tracking_number}")
            if code.get("number") != code_number or tracking.get("number") != tracking_number:
                raise Pending("accepted child PR receipt number mismatch")
            receipt(code, child["sha"], code_branch)
            tracking_sha = tracking.get("head", {}).get("sha", "")
            if not re.fullmatch(r"[0-9a-f]{40}", tracking_sha):
                raise Pending("accepted child tracking head missing")
            receipt(tracking, tracking_sha, audit["tracking_branch"])
            if code["merge_commit_sha"] != recorded["merge_commit_sha"]:
                raise Pending("accepted child code merge changed")
            for revision in (child["sha"], tracking_sha, code["merge_commit_sha"], tracking["merge_commit_sha"]):
                git(checkout, "fetch", "origin", revision)
            code_merge, tracking_merge = code["merge_commit_sha"], tracking["merge_commit_sha"]
            for ancestor, descendant in ((code_merge, tracking_merge), (tracking_merge, job["sha"]),
                                         (code_merge, tracking_sha)):
                git(checkout, "merge-base", "--is-ancestor", ancestor, descendant)
            if code_merge == tracking_merge:
                raise Pending("accepted child needs separate code and tracking merges")
            for revision in (tracking_sha, tracking_merge):
                changes = set(git(checkout, "diff", "--name-only", revision + "^", revision).splitlines())
                if changes != {plan, audit_path} or git(checkout, "ls-tree", revision + "^", "--", audit_path):
                    raise Pending("accepted child tracking receipt does not introduce only its plan/audit acceptance")
            def content(revision, path):
                return run(["git", "show", f"{revision}:{path}"], checkout, strip=False)
            if (content(tracking_merge, audit_path) != content(job["sha"], audit_path) or
                    content(tracking_sha, audit_path) != content(tracking_merge, audit_path) or
                    content(tracking_merge, plan) != content(job["sha"], plan) or
                    content(tracking_sha, plan) != content(tracking_merge, plan)):
                raise Pending("accepted child plan/audit changed after acceptance")
            if accepted_plan(content(child["sha"], plan)) != content(tracking_merge, plan):
                raise Pending("accepted child plan changed during acceptance")
            self.evidence(directory, child, supplied=audit["evidence"])
            accepted = self.ticket(checkout, child, tracking_merge)
            current = self.ticket(checkout, child, job["sha"])
            if accepted.get("status") != "done" or current.get("status") != "done":
                raise Pending("accepted child is not done")
            audited = self.ticket(checkout, child, child["sha"])
            base = self.ticket(checkout, child, base_sha,
                               after_proof=lambda dependency, satisfied: self.accepted_child(
                                   directory, job, dependency, base_sha, visiting, base_satisfied=satisfied))
            fields = ("description", "verify", "story_file", "hitl", "done_checkpoint", "after", "gated_by", "unknown")
            for found in (accepted, current, base):
                if any(audited.get(field) != found.get(field) for field in fields):
                    raise Pending("accepted child semantic contract changed")
            if audited.get("story_file"):
                path = relative(checkout, audited["story_file"])
                if any(content(child["sha"], path) != content(revision, path)
                       for revision in (tracking_merge, job["sha"], base_sha)):
                    raise Pending("accepted child story contract changed")
            return True
        except (KeyError, TypeError, ValueError, AttributeError, IndexError, RecursionError, RuntimeError) as error:
            raise Pending(f"accepted child proof incomplete: {error}") from error
        finally:
            visiting.remove(key)
            git(checkout, "checkout", "--detach", base_sha)

    def current_ticket(self, directory, job, sha):
        checkout = directory / "checkout"
        source = self.ticket(checkout, job, job["sha"])
        git(checkout, "fetch", "origin", job["base"])
        base_sha = git(checkout, "rev-parse", "FETCH_HEAD")
        after_proof = None
        if job.get("source_branch") and job["source_branch"] != job["base"]:
            visiting = set()
            after_proof = lambda row, satisfied: self.accepted_child(
                directory, job, row, base_sha, visiting, base_satisfied=satisfied)
        current = self.ticket(checkout, job, base_sha,
                              after_proof=after_proof)
        for field in ("description", "verify", "story_file", "hitl", "done_checkpoint", "after", "gated_by", "unknown"):
            if source.get(field) != current.get(field):
                raise Pending("ticket contract or gates changed on base")
        if source.get("story_file"):
            path = relative(checkout, source["story_file"])
            if git(checkout, "show", f"{job['sha']}:{path}") != git(checkout, "show", f"{base_sha}:{path}"):
                raise Pending("ticket story changed on base")
        def plan_at(revision):
            if not git(checkout, "ls-tree", revision, "--", job["plan"]):
                return None
            return git(checkout, "show", f"{revision}:{job['plan']}")
        pinned_plan, current_plan = plan_at(job["sha"]), plan_at(base_sha)
        # A new plan need not exist on main before the code PR lands.
        baseline = git(checkout, "merge-base", job["sha"], base_sha)
        unchanged_baseline = not job.get("code_pr") and current_plan == plan_at(baseline)
        if not unchanged_baseline:
            if current_plan is None:
                raise Pending("native plan removed from base")
            status = re.search(r"(?m)^status: *[\"']?([\w-]+)", current_plan.split("---", 2)[1])
            if not status or status[1] not in ("built", "done"):
                raise Pending("native plan has a newer build/review state")
            def comparable(text):
                frontmatter, body = text.split("---", 2)[1:]
                return re.sub(r"(?m)^status:.*$", "status: built", frontmatter) + "---" + body
            if comparable(pinned_plan) != comparable(current_plan):
                raise Pending("native plan changed on base")
        git(checkout, "checkout", "--detach", sha)
        return base_sha

    def native(self, directory, job, sha, stage):
        record = directory / f"{stage}-checks.json"
        if record.exists() and read(record).get("sha") == sha:
            return read(record)
        checkout = directory / "checkout"
        if sys.platform != "darwin":
            raise Pending("native macOS checks required")
        env = dict(os.environ, WXT_PREMIUM_API_URL="http://localhost:8787", CI="true")
        if not re.match(r"v22\.", run(["node", "--version"], checkout)):
            raise Pending("select Node 22 before starting worker")
        if run(["pnpm", "--version"], checkout) != "10.17.1":
            raise Pending("pnpm 10.17.1 required")
        results = []
        for command in (["pnpm", "install", "--frozen-lockfile"], ["pnpm", "lint"],
                        ["pnpm", "typecheck"], ["pnpm", "test"]):
            output = run(command, checkout, timeout=self.cfg.get("check_timeout", 1800), env=env)
            results.append(dict(command=command, status="passed", output_tail=output[-2000:]))
        if git(checkout, "rev-parse", "HEAD") != sha or git(checkout, "status", "--porcelain"):
            raise Pending("checks changed tracked source or HEAD")
        result = dict(sha=sha, platform=sys.platform, results=results)
        atomic(record, result)
        return result

    def checks(self, job, sha, base):
        repo = job["repository"]
        actions = self.api(f"repos/{repo}/actions/permissions")
        if type(actions.get("enabled")) is not bool:
            raise Pending("Actions permission unreadable")
        encoded_base = quote(base, safe="")
        protection = self.api(f"repos/{repo}/branches/{encoded_base}")
        if type(protection.get("protected")) is not bool:
            raise Pending("branch protection unreadable")
        expected = set()
        if protection["protected"]:
            rules = self.api(f"repos/{repo}/branches/{encoded_base}/protection")
            required = rules.get("required_status_checks") or {}
            expected.update(required.get("contexts", []))
            expected.update(c["context"] for c in required.get("checks", []))
        runs = self.pages(f"repos/{repo}/actions/runs?head_sha={sha}", "workflow_runs") if actions["enabled"] else []
        latest_runs = {}
        for workflow in runs:
            latest_runs.setdefault((workflow["workflow_id"], workflow.get("event")), workflow)
        for workflow in latest_runs.values():
            if workflow["status"] != "completed" or workflow.get("conclusion") not in ("success", "neutral", "skipped"):
                raise Pending(f"Actions workflow waiting or failed: {workflow.get('name', workflow['workflow_id'])}")
        checks = self.pages(f"repos/{repo}/commits/{sha}/check-runs?filter=latest", "check_runs")
        statuses = self.pages(f"repos/{repo}/commits/{sha}/statuses")
        seen = set()
        for check in checks:
            seen.add(check["name"])
            if check["status"] != "completed" or check.get("conclusion") not in ("success", "neutral", "skipped"):
                raise Pending(f"check waiting or failed: {check['name']}")
        latest = {}
        for status in statuses:
            latest.setdefault(status["context"], status)
        for name, status in latest.items():
            seen.add(name)
            if status["state"] != "success":
                raise Pending(f"status waiting or failed: {name}")
        # Disabled Actions do not waive required branch checks.
        required_names = expected | (set(self.cfg["expected_checks"]) if actions["enabled"] else set())
        if actions["enabled"] and not self.cfg["expected_checks"]:
            raise Pending("enabled Actions require explicit expected_checks")
        if not required_names <= seen:
            raise Pending("expected checks not registered: " + ", ".join(sorted(required_names - seen)))
        return dict(actions="enabled" if actions["enabled"] else "disabled", checks=checks,
                    statuses=list(latest.values()), workflows=list(latest_runs.values()), protection=protection)

    def pr(self, job, branch):
        owner = job["repository"].split("/")[0]
        encoded_base = quote(job["base"], safe="")
        prs = self.pages(f"repos/{job['repository']}/pulls?state=all&head={owner}:{branch}&base={encoded_base}")
        if len(prs) > 1:
            raise Pending("multiple matching PRs require operator resolution")
        return prs[0] if prs else None

    def deliver(self, directory, job, sha, branch, stage):
        checkout = directory / "checkout"
        pr = self.pr(job, branch)
        if pr and pr["head"]["sha"] != sha:
            raise Pending("PR head changed; new evidence required")
        if pr and pr.get("merged_at"):
            verification = job.get(f"{stage}_verification")
            if not isinstance(verification, dict) or verification.get("sha") != sha:
                raise Pending("merged PR lacks saved exact-head verification")
            check_native(verification.get("local"), sha)
            full = self.api(f"repos/{job['repository']}/pulls/{pr['number']}")
            return self.confirm_merge(job, sha, full)
        if pr and pr["state"] != "open":
            raise Pending("PR closed without merge")
        self.current_ticket(directory, job, sha)
        check_source(checkout, sha)
        local = self.native(directory, job, sha, stage)
        check_native(local, sha)
        check_source(checkout, sha)
        if not pr:
            # Native install may replace the hook; validate the effective hook at push time.
            hook = Path(git(checkout, "rev-parse", "--path-format=absolute", "--git-path", "hooks/pre-push"))
            if self.push_policy == "hook-required" and (not hook.is_file() or not os.access(hook, os.X_OK)):
                raise Pending("required pre-push hook is absent or not executable")
            run(["git", "push", "origin", f"{sha}:refs/heads/{branch}"], checkout,
                timeout=self.cfg.get("check_timeout", 1800),
                env=dict(os.environ, WXT_PREMIUM_API_URL="http://localhost:8787"))
            check_source(checkout, sha)
            body = directory / f"{stage}-body.md"
            body.write_text(f"Deliver ticket {job['ticket']} ({stage}).\n\nVerified source: {sha}\nDelivery job: {job['id']}\n")
            self.gh("pr", "create", "--repo", job["repository"], "--head", branch,
                    "--base", job["base"], "--title", f"{job['ticket']}: {job['title']} ({stage})", "--body-file", str(body))
            pr = self.pr(job, branch)
            if not pr:
                raise Pending("PR creation not yet visible")
        checks = self.checks(job, sha, job["base"])
        view = json.loads(self.gh("pr", "view", str(pr["number"]), "--repo", job["repository"],
                                  "--json", "headRefOid,baseRefName,mergeStateStatus,reviewDecision,isDraft"))
        if view["headRefOid"] != sha or view["baseRefName"] != job["base"]:
            raise Pending("PR head/base changed")
        if view["isDraft"] or view["reviewDecision"] in ("REVIEW_REQUIRED", "CHANGES_REQUESTED") or view["mergeStateStatus"] != "CLEAN":
            raise Pending("GitHub protection/review/mergeability gate")
        self.evidence(directory, job)
        self.current_ticket(directory, job, sha)
        check_source(checkout, sha)
        job[f"{stage}_verification"] = dict(local=local, github=checks, sha=sha)
        self.save(directory, job)
        self.gh("pr", "merge", str(pr["number"]), "--repo", job["repository"],
                "--squash", "--match-head-commit", sha)
        full = self.api(f"repos/{job['repository']}/pulls/{pr['number']}")
        return self.confirm_merge(job, sha, full)

    def confirm_merge(self, job, sha, full):
        if not full.get("merged"):
            raise Pending("merge requested; waiting for confirmed merge/queue")
        if (not re.fullmatch(r"[0-9a-f]{40}", full.get("merge_commit_sha") or "") or
                full.get("head", {}).get("sha") != sha or
                full.get("base", {}).get("ref") != job["base"] or
                full.get("base", {}).get("repo", {}).get("full_name") != job["repository"] or
                full.get("head", {}).get("repo", {}).get("full_name", job["repository"]) != job["repository"]):
            raise Pending("merge identity not confirmed")
        return full

    def tracking(self, directory, job, evidence):
        checkout = directory / "checkout"
        ref = f"refs/delivery/{job['id']}-tracking"
        try:
            sha = git(checkout, "rev-parse", "--verify", ref)
            return sha
        except RuntimeError:
            pass
        base_sha = self.current_ticket(directory, job, job["sha"])
        git(checkout, "checkout", "--detach", base_sha)
        before = git(checkout, "rev-parse", "HEAD")
        script = self.cfg.get("tickets_script", ".agents/skills/bmad-agent-po/scripts/tickets.py")
        found = json.loads(run([sys.executable, script, "--project-root", str(checkout),
                               "find", job["container"], job["ticket"]], checkout))
        # The supported command must still resolve the pinned plan, never another ticket.
        if relative(checkout, found["plan"]) != job["plan"]:
            raise Pending("ticket plan binding changed")
        if found.get("hitl") or found.get("done_checkpoint"):
            raise Pending("current ticket requires human clearance")
        run([sys.executable, script, "--project-root", str(checkout),
             "mark", job["container"], job["ticket"], "done"], checkout)
        audit = Path(job["plan"]).parent / f"delivery-{job['id']}.json"
        atomic(checkout / audit, dict(job=job, evidence=evidence, code_pr=job["code_pr"],
               tracking_branch=f"delivery/{job['id']}-tracking",
               tracking_audit="Tracking PR and confirmed merge are recorded in external job.json"))
        changed = set(git(checkout, "diff", "--name-only").splitlines())
        if not changed <= {job["plan"]}:
            raise Pending("Ticket Tree modified files outside the bound plan")
        git(checkout, "add", "--", job["plan"], str(audit))
        git(checkout, "commit", "-m", f"Accept {job['ticket']} after verified code delivery")
        sha = git(checkout, "rev-parse", "HEAD")
        git(checkout, "update-ref", ref, sha)
        job["tracking_base"] = before
        return sha

    def initialize(self, directory, job):
        checkout = directory / "checkout"
        if not checkout.exists():
            temporary = directory / "clone-partial"
            if temporary.exists():
                import shutil
                shutil.rmtree(temporary)
            run(["git", "clone", "--no-hardlinks", "--no-checkout", job["source_gitdir"], str(temporary)])
            git(temporary, "remote", "set-url", "origin", job["origin"])
            for field, value in job["git_settings"].items():
                git(temporary, "config", field, value)
            temporary.rename(checkout)
        pin = f"refs/delivery/{job['id']}"
        source = run(["git", "--git-dir", job["source_gitdir"], "rev-parse", "--verify", pin])
        if source != job["sha"]:
            raise Pending("durable source pin changed")
        git(checkout, "fetch", job["source_gitdir"], pin)
        if git(checkout, "rev-parse", "FETCH_HEAD") != job["sha"]:
            raise Pending("fetched source pin mismatch")

    def advance(self, directory, job):
        self.initialize(directory, job)
        checkout = directory / "checkout"
        # Recover a crash during mark/audit before its commit, only in our clone.
        if (checkout / job["plan"]).exists():
            git(checkout, "restore", "--source=HEAD", "--staged", "--worktree", "--", job["plan"])
        audit = Path(job["plan"]).parent / f"delivery-{job['id']}.json"
        if (checkout / audit).exists() and str(audit) in git(checkout, "ls-files", "--others", "--exclude-standard").splitlines():
            (checkout / audit).unlink()
        evidence = self.evidence(directory, job)
        actual_repo = json.loads(self.gh("repo", "view", github_origin(job["origin"], job["repository"], checkout),
                                         "--json", "nameWithOwner"))
        if actual_repo.get("nameWithOwner") != job["repository"]:
            raise Pending("origin does not match authorized repository")
        if job["stage"] == "code":
            job["code_pr"] = self.deliver(directory, job, job["sha"], f"delivery/{job['id']}-code", "code")
            job["stage"] = "tracking"
            self.save(directory, job)
        sha = self.tracking(directory, job, evidence)
        job["tracking_sha"] = sha
        self.save(directory, job)
        job["tracking_pr"] = self.deliver(directory, job, sha, f"delivery/{job['id']}-tracking", "tracking")
        job.update(stage="complete", status="complete", error="")
        self.save(directory, job)

    def process(self, path):
        with lock(path.parent / "job.lock") as held:
            if not held:
                return False
            job = read(path)
            if job["stage"] == "complete" or job.get("status") == "retry-required" or job.get("next_try", 0) > time.time():
                return False
            try:
                self.advance(path.parent, job)
                return True
            except Exception as error:
                job["attempts"] += 1
                job["error"] = f"{type(error).__name__}: {error}"
                job["status"] = "waiting" if isinstance(error, Pending) else "error"
                job["next_try"] = time.time() + min(300, 2 ** min(job["attempts"], 8))
                if job["attempts"] >= self.cfg.get("max_attempts", 20):
                    job["next_try"] = 0
                    job["status"] = "retry-required"
                self.save(path.parent, job)
                return False

    def pass_once(self):
        paths = sorted(Path(self.cfg["state_dir"]).glob("*/job.json"))
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            return any(list(pool.map(self.process, paths)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("worker")
    sub.add_parser("status")
    retry = sub.add_parser("retry")
    retry.add_argument("job")
    ev = sub.add_parser("evidence")
    ev.add_argument("job")
    ev.add_argument("file")
    rec = sub.add_parser("reconcile")
    rec.add_argument("repository_root")
    rec.add_argument("run_dir")
    args = parser.parse_args()
    cfg = config(args.config)
    root = Path(cfg["state_dir"])
    if args.command == "worker":
        with lock(root / "worker.lock") as held:
            if held:
                worker = Worker(cfg)
                deadline = time.monotonic() + cfg.get("worker_lifetime", 3600)
                while time.monotonic() < deadline:
                    worker.pass_once()
                    active = [read(p) for p in root.glob("*/job.json")]
                    if not any(j["stage"] != "complete" and j.get("status") != "retry-required" for j in active):
                        break
                    time.sleep(min(cfg.get("poll_interval", 5), 30))
    elif args.command == "status":
        print(json.dumps([read(p) for p in sorted(root.glob("*/job.json"))], indent=2))
    elif args.command == "reconcile":
        reconcile(cfg, args.repository_root, args.run_dir)
    else:
        if not re.fullmatch(r"[0-9a-f]{24}", args.job):
            parser.error("invalid job ID")
        directory = root / args.job
        with lock(directory / "job.lock", blocking=True):
            job = read(directory / "job.json")
            if args.command == "evidence":
                atomic(directory / "evidence.json", read(args.file))
                worker = Worker(cfg)
                worker.initialize(directory, job)
                worker.evidence(directory, job)
            job.update(attempts=0, next_try=0, status="queued", error="")
            atomic(directory / "job.json", job)
        print("Queued; run worker to restart delivery.")


if __name__ == "__main__":
    main()
