"""Provider read containment. These tests execute the shipped seatbelt."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
import uuid
from pathlib import Path

from nightshift.config import load_config
from nightshift.containment import (
    build_read_policy,
    contained_run,
    profile_has_global_file_read,
    write_profile,
)
from nightshift.db import Database
from nightshift.job import JobValidationError, load_job
from nightshift.locks import LockManager
from nightshift.models import RunState
from nightshift.policy import build_policy, render_preamble
from nightshift.queue import enqueue
from nightshift.runner import execute_run
from nightshift.runtime import (
    auth_bootstrap,
    copy_auth_file,
    ensure_auth_store,
    legacy_profile_dir,
    prepare_run_grok_home,
    scrub_per_run_auth,
)
from nightshift.testkit import make_repo, write_job
from nightshift.workspace import inspect_source, prepare_workspace
import nightshift


def _env(home: Path) -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(home)}


def _saw(profile: Path, cwd: Path, env: dict[str, str], target: Path, needle: str) -> bool:
    quoted = str(target)
    commands = [
        ["cat", quoted],
        ["/bin/cat", quoted],
        ["/usr/bin/python3", "-c", f"print(open({quoted!r}).read())"],
        ["/bin/sh", "-c", f"cat {quoted}"],
    ]
    for argv in commands:
        _code, output = contained_run(argv, cwd=cwd, env=env, profile=profile, timeout=30)
        if needle in output:
            return True
    return False


class ReadContainmentTests(unittest.TestCase):
    def test_home_and_source_reads_are_blocked_and_workspace_reads_work(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            workspace = root / "clone"
            make_repo(source)
            (source / "SOURCE_PRIVATE_SENTINEL.txt").write_text("synthetic-source-sentinel\n", encoding="utf-8")
            prepare_workspace(inspect_source(source, "HEAD"), workspace, "clone")
            self.assertFalse((workspace / "SOURCE_PRIVATE_SENTINEL.txt").exists())
            self.assertTrue((workspace / "README.md").is_file())
            runtime = root / "run"
            runtime.mkdir()
            profile = write_profile(
                runtime / "provider.sb",
                writable=[workspace, runtime],
                network=False,
                read_policy=build_read_policy(runtime_roots=[workspace, runtime], source_root=source),
            )
            text = (runtime / "provider.sb").read_text(encoding="utf-8")
            self.assertFalse(profile_has_global_file_read(text))
            self.assertIn("(deny default)", text)
            home = str(Path.home().resolve())
            self.assertLess(text.index(f'(deny file-read* (subpath "{home}"))'), text.index("; re-allow"))
            env = _env(runtime)
            readme = (workspace / "README.md").read_text(encoding="utf-8")
            code, output = contained_run(
                ["/bin/cat", str(workspace / "README.md")],
                cwd=workspace,
                env=env,
                profile=profile,
                timeout=30,
            )
            self.assertEqual(code, 0, output)
            self.assertIn(readme, output)
            self.assertFalse(
                _saw(profile, workspace, env, source / "SOURCE_PRIVATE_SENTINEL.txt", "synthetic-source-sentinel")
            )
            code, output = contained_run(
                ["/bin/echo", "nightshift-system-ok"],
                cwd=workspace,
                env=env,
                profile=profile,
                timeout=30,
            )
            self.assertEqual(code, 0, output)
            self.assertIn("nightshift-system-ok", output)
            canary = Path.home() / f"nightshift-read-canary-{uuid.uuid4().hex}"
            needle = f"synthetic-operator-home-canary-{canary.name}"
            try:
                canary.mkdir(mode=0o700)
                target = canary / "outside-private.txt"
                target.write_text(needle + "\n", encoding="utf-8")
                self.assertFalse(_saw(profile, workspace, env, target, needle))
            finally:
                if canary.exists():
                    for child in canary.iterdir():
                        child.unlink()
                    canary.rmdir()

    def test_ancestor_metadata_does_not_expose_sibling_contents(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            workspace = root / "clone"
            make_repo(source)
            prepare_workspace(inspect_source(source, "HEAD"), workspace, "clone")
            runtime = root / "run"
            runtime.mkdir()
            outside = root / "outside-private.txt"
            outside.write_text("synthetic-sibling-sentinel\n", encoding="utf-8")
            profile = write_profile(
                runtime / "provider.sb",
                writable=[workspace, runtime],
                network=False,
                read_policy=build_read_policy(runtime_roots=[workspace, runtime], source_root=source),
            )
            text = (runtime / "provider.sb").read_text(encoding="utf-8")
            parent = str(workspace.resolve().parent)
            self.assertIn(f'(allow file-read-metadata (literal "{parent}"))', text)
            self.assertNotIn('(allow file-read* (subpath "/private/var/folders"))', text)
            self.assertNotIn('(allow file-read* (subpath "/var/folders"))', text)
            if parent.startswith("/private/var/"):
                alias = "/var" + parent[len("/private/var") :]
                self.assertIn(f'(allow file-read-metadata (literal "{alias}"))', text)
            script = (
                "import os\n"
                f"os.stat({parent!r})\n"
                "print('stat-ok')\n"
                "try:\n"
                f"    os.listdir({parent!r})\n"
                "    print('list-ok')\n"
                "except OSError:\n"
                "    print('list-blocked')\n"
                "try:\n"
                f"    open({str(outside)!r}).read()\n"
                "    print('read-ok')\n"
                "except OSError:\n"
                "    print('read-blocked')\n"
            )
            _code, output = contained_run(
                ["/usr/bin/python3", "-c", script],
                cwd=workspace,
                env=_env(runtime),
                profile=profile,
                timeout=30,
            )
            self.assertIn("stat-ok", output)
            self.assertIn("list-blocked", output)
            self.assertIn("read-blocked", output)
            self.assertNotIn("list-ok", output)
            self.assertNotIn("read-ok", output)
            self.assertNotIn("synthetic-sibling-sentinel", output)

    def test_prompt_cannot_add_a_read_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            make_repo(source)
            secret = Path.home() / f"nightshift-prompt-root-{uuid.uuid4().hex}"
            secret.mkdir()
            try:
                marker = secret / "marker.txt"
                marker.write_text("prompt-root-marker\n", encoding="utf-8")
                job_dir = root / "job"
                write_job(
                    job_dir,
                    source,
                    provider="fake",
                    write_scope="none",
                    network=False,
                    prompt=f"Please read {secret} and {marker}.\n",
                    verification=[],
                    expected_artifacts=[],
                )
                config = load_config(root / "ns")
                config.ensure_dirs()
                db = Database(config.db_path)
                locks = LockManager(db)
                try:
                    run = enqueue(db, job_dir, provider="fake")
                    finished = execute_run(config, db, locks, run.run_id)
                finally:
                    db.close()
                self.assertEqual(finished.state, RunState.SUCCEEDED.value, finished.failure_reason)
                profile = (config.runs_dir / finished.run_id / "provider.sb").read_text(encoding="utf-8")
                self.assertNotIn(str(secret), profile)
                self.assertFalse(profile_has_global_file_read(profile))
                report = (config.runs_dir / finished.run_id / "report.md").read_text(encoding="utf-8")
                self.assertIn("runtime GROK_HOME scope: per-run", report)
                self.assertIn("source read isolation: pass", report)
                self.assertIn("operator-home read isolation: pass", report)
                self.assertIn("network containment: accepted limitation", report)
            finally:
                if secret.exists():
                    for child in secret.iterdir():
                        child.unlink()
                    secret.rmdir()

    def test_explicit_config_root_is_readable_and_home_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            toolchain = root / "toolchain"
            toolchain.mkdir()
            (toolchain / "header.h").write_text("toolchain-header\n", encoding="utf-8")
            config_dir = root / "ns"
            config_dir.mkdir()
            (config_dir / "nightshift.toml").write_text(
                f'[containment]\nread_roots = ["{toolchain}"]\n',
                encoding="utf-8",
            )
            config = load_config(config_dir)
            self.assertEqual(config.explicit_read_roots, (toolchain.resolve(),))
            workspace = root / "workspace"
            workspace.mkdir()
            profile = write_profile(
                root / "provider.sb",
                writable=[workspace],
                network=False,
                read_policy=build_read_policy(
                    runtime_roots=[workspace],
                    explicit_read_roots=list(config.explicit_read_roots),
                ),
            )
            code, output = contained_run(
                ["/bin/cat", str(toolchain / "header.h")],
                cwd=workspace,
                env=_env(workspace),
                profile=profile,
                timeout=30,
            )
            self.assertEqual(code, 0, output)
            self.assertIn("toolchain-header", output)
            with self.assertRaises(Exception):
                build_read_policy(runtime_roots=[workspace], explicit_read_roots=[Path.home()])


class RuntimeProfileTests(unittest.TestCase):
    def test_per_run_homes_do_not_share_mutations_and_auth_is_scrubbed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            operator = root / "operator"
            operator.mkdir()
            (operator / "auth.json").write_bytes(b"synthetic-auth-material")
            (operator / "config.toml").write_text("operator-config\n", encoding="utf-8")
            legacy = legacy_profile_dir(root / "state")
            legacy.mkdir(parents=True)
            (legacy / "auth.json").write_bytes(b"legacy-auth-material")
            store = ensure_auth_store(root / "state")
            self.assertEqual((store / "auth.json").read_bytes(), b"legacy-auth-material")
            self.assertTrue((legacy / "auth.json").is_file())
            message = auth_bootstrap(store, source_home=operator)
            self.assertEqual(message, "bootstrapped")
            self.assertEqual((store / "auth.json").read_bytes(), b"synthetic-auth-material")
            self.assertFalse((store / "config.toml").exists())
            self.assertEqual(stat.S_IMODE(store.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((store / "auth.json").stat().st_mode), 0o600)
            before = (store / "auth.json").read_bytes()
            home_a = prepare_run_grok_home(root / "run-a", store)
            home_b = prepare_run_grok_home(root / "run-b", store)
            self.assertNotEqual(home_a.resolve(), home_b.resolve())
            self.assertEqual((home_a / "auth.json").read_bytes(), before)
            self.assertEqual(stat.S_IMODE((home_a / "auth.json").stat().st_mode), 0o600)
            self.assertFalse((home_a / "auth.json").is_symlink())
            (home_a / "config.toml").write_text("run-a-extension\n", encoding="utf-8")
            self.assertFalse((home_b / "config.toml").exists())
            self.assertEqual((store / "auth.json").read_bytes(), before)
            self.assertTrue(scrub_per_run_auth(root / "run-a"))
            self.assertFalse((home_a / "auth.json").exists())
            self.assertEqual((home_a / "config.toml").read_text(encoding="utf-8"), "run-a-extension\n")
            self.assertEqual((store / "auth.json").read_bytes(), before)
            self.assertFalse(scrub_per_run_auth(root / "run-a"))

    def test_provider_cannot_write_the_persistent_auth_store(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = root / "credentials" / "grok"
            store.mkdir(parents=True)
            target = store / "auth.json"
            copy_auth_file(self._source(root), target)
            before = target.read_bytes()
            workspace = root / "workspace"
            workspace.mkdir()
            profile = write_profile(
                root / "provider.sb",
                writable=[workspace],
                network=False,
                read_policy=build_read_policy(runtime_roots=[workspace], source_root=root / "source"),
            )
            contained_run(
                ["/usr/bin/python3", "-c", f"open({str(target)!r},'w').write('stolen')"],
                cwd=workspace,
                env=_env(workspace),
                profile=profile,
                timeout=30,
            )
            self.assertEqual(target.read_bytes(), before)

    def test_interrupted_recovery_scrubs_per_run_auth_only(self) -> None:
        import subprocess

        from nightshift.models import empty_run
        from nightshift.supervisor import recover_run

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            operator = root / "operator"
            operator.mkdir()
            (operator / "auth.json").write_bytes(b"synthetic-auth-material")
            config = load_config(root / "ns")
            config.ensure_dirs()
            auth_bootstrap(config.auth_store(), source_home=operator)
            before = (config.auth_store() / "auth.json").read_bytes()
            db = Database(config.db_path)
            locks = LockManager(db)
            workspace = root / "workspace"
            make_repo(workspace)
            dead = subprocess.Popen(["true"])
            dead.wait(timeout=5)
            try:
                run = db.insert_run(
                    empty_run(
                        job_id="gone",
                        provider="fake",
                        state=RunState.RUNNING.value,
                        attempt=1,
                        max_attempts=1,
                        pid=dead.pid,
                        process_meta={"match": "nightshift-not-running", "pid": dead.pid},
                        job_snapshot=(
                            '{"schema_version":1,"id":"gone","description":"gone","type":"note",'
                            '"repository":"/tmp/unused","base_ref":"HEAD","provider":"fake","model":null,'
                            '"max_runtime_seconds":30,"max_attempts":1,"concurrency_group":"gone",'
                            '"network":false,"write_scope":"none","expected_artifacts":[],"verification":[],'
                            '"success_criteria":["kept"],"allow_bash":[],"prompt":"noop\\n","job_dir":"/tmp",'
                            '"job_file":"/tmp/job.toml"}'
                        ),
                        workspace_path=str(workspace),
                        source_repo=str(workspace),
                        run_dir=str(config.runs_dir / "gone"),
                    )
                )
                home = prepare_run_grok_home(Path(run.run_dir), config.auth_store())
                (home / "config.toml").write_text("keep-diagnostic\n", encoding="utf-8")
                updated, applied = recover_run(config, db, locks, run.run_id)
            finally:
                db.close()
            self.assertEqual(applied.classification, "process_gone_workspace_intact")
            self.assertEqual(updated.state, RunState.INTERRUPTED.value)
            self.assertFalse(applied.launch_provider)
            self.assertFalse((home / "auth.json").exists())
            self.assertEqual((home / "config.toml").read_text(encoding="utf-8"), "keep-diagnostic\n")
            self.assertEqual((config.auth_store() / "auth.json").read_bytes(), before)

    def test_verification_recovery_does_not_leave_per_run_auth(self) -> None:
        import subprocess

        from nightshift.integrity import capture, dumps
        from nightshift.models import empty_run
        from nightshift.supervisor import recover_run

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            operator = root / "operator"
            operator.mkdir()
            (operator / "auth.json").write_bytes(b"synthetic-auth-material")
            config = load_config(root / "ns")
            config.ensure_dirs()
            auth_bootstrap(config.auth_store(), source_home=operator)
            before = (config.auth_store() / "auth.json").read_bytes()
            db = Database(config.db_path)
            locks = LockManager(db)
            workspace = root / "verify-ws"
            make_repo(workspace)
            (workspace / "marker.txt").write_text("ok\n", encoding="utf-8")
            dead = subprocess.Popen(["true"])
            dead.wait(timeout=5)
            try:
                run = db.insert_run(
                    empty_run(
                        job_id="verify-auth",
                        provider="fake",
                        state=RunState.RUNNING.value,
                        attempt=1,
                        max_attempts=1,
                        pid=dead.pid,
                        process_meta={"match": "gone-provider", "pid": dead.pid},
                        provider_exit_code=0,
                        verification_ran=False,
                        job_snapshot=(
                            '{"schema_version":1,"id":"verify-auth","description":"recover","type":"note",'
                            '"repository":"/tmp/unused","base_ref":"HEAD","provider":"fake","model":null,'
                            '"max_runtime_seconds":30,"max_attempts":1,"concurrency_group":"verify-auth",'
                            '"network":false,"write_scope":"none","expected_artifacts":[],'
                            '"verification":["test -f marker.txt"],"success_criteria":["kept"],'
                            '"allow_bash":[],"prompt":"noop\\n","job_dir":"/tmp","job_file":"/tmp/job.toml"}'
                        ),
                        workspace_path=str(workspace),
                        source_repo=str(workspace),
                        source_revision="unused",
                        run_dir=str(config.runs_dir / "verify-auth"),
                        source_head="unused",
                        base_ref="HEAD",
                        source_integrity=dumps(capture(workspace)),
                    )
                )
                home = prepare_run_grok_home(Path(run.run_dir), config.auth_store())
                self.assertTrue((home / "auth.json").is_file())
                updated, applied = recover_run(config, db, locks, run.run_id)
            finally:
                db.close()
            self.assertEqual(applied.classification, "verification_never_ran")
            self.assertFalse(applied.launch_provider)
            self.assertTrue(updated.verification_ran)
            self.assertFalse((home / "auth.json").exists())
            self.assertEqual((config.auth_store() / "auth.json").read_bytes(), before)

    def test_happy_path_verification_cannot_read_per_run_auth(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            operator = root / "operator"
            operator.mkdir()
            (operator / "auth.json").write_bytes(b"synthetic-auth-material")
            source = root / "source"
            make_repo(source)
            job_dir = root / "job"
            write_job(
                job_dir,
                source,
                provider="fake",
                write_scope="none",
                network=False,
                expected_artifacts=[],
                success_criteria=["Verification does not see the auth copy."],
                verification=[
                    "/usr/bin/python3 -c 'import os; p=os.path.join(os.environ[\"GROK_HOME\"], \"auth.json\"); print(\"present\" if os.path.isfile(p) else \"missing\")'"
                ],
            )
            config = load_config(root / "ns")
            config.ensure_dirs()
            auth_bootstrap(config.auth_store(), source_home=operator)
            before = (config.auth_store() / "auth.json").read_bytes()
            self.assertEqual(before, b"synthetic-auth-material")
            db = Database(config.db_path)
            locks = LockManager(db)
            try:
                run = enqueue(db, job_dir, provider="fake")
                finished = execute_run(config, db, locks, run.run_id)
            finally:
                db.close()
            log = (config.runs_dir / finished.run_id / "verification.log").read_text(encoding="utf-8")
            self.assertEqual(finished.state, RunState.SUCCEEDED.value, finished.failure_reason)
            self.assertRegex(log, r"(?m)^missing$")
            self.assertNotRegex(log, r"(?m)^present$")
            self.assertFalse((config.runs_dir / finished.run_id / "grok-home" / "auth.json").exists())
            self.assertEqual((config.auth_store() / "auth.json").read_bytes(), before)

    def test_read_only_run_restores_neutralized_clone_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            make_repo(source)
            (source / ".mcp.json").write_text('{"mcpServers":{}}\n', encoding="utf-8")
            skill = source / ".claude" / "skills" / "emitting-output"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("synthetic skill\n", encoding="utf-8")
            git = __import__("subprocess")
            git.check_call(["git", "-C", str(source), "add", "-A"])
            git.check_call(["git", "-C", str(source), "commit", "-m", "extensions"])
            job_dir = root / "job"
            write_job(
                job_dir,
                source,
                provider="fake",
                write_scope="none",
                network=False,
                verification=[],
                expected_artifacts=[],
            )
            config = load_config(root / "ns")
            config.ensure_dirs()
            db = Database(config.db_path)
            locks = LockManager(db)
            try:
                run = enqueue(db, job_dir, provider="fake")
                finished = execute_run(config, db, locks, run.run_id)
            finally:
                db.close()
            self.assertEqual(finished.state, RunState.SUCCEEDED.value, finished.failure_reason)
            workspace = Path(finished.workspace_path)
            status = git.check_output(
                ["git", "-C", str(workspace), "status", "--porcelain=v1"],
                text=True,
            )
            self.assertEqual(status, "")
            self.assertEqual((workspace / ".mcp.json").read_text(encoding="utf-8"), '{"mcpServers":{}}\n')
            self.assertEqual((workspace / ".claude" / "skills" / "emitting-output" / "SKILL.md").read_text(encoding="utf-8"), "synthetic skill\n")
            self.assertTrue((source / ".mcp.json").is_file())
            report = (config.runs_dir / finished.run_id / "report.md").read_text(encoding="utf-8")
            self.assertIn("(no file changes detected)", report)
            self.assertFalse((config.runs_dir / finished.run_id / "grok-home" / "auth.json").exists())

    def _source(self, root: Path) -> Path:
        origin = root / "origin"
        origin.mkdir()
        path = origin / "auth.json"
        path.write_bytes(b"synthetic-auth-material")
        return path


class WordingTests(unittest.TestCase):
    def test_version_stays_0_2_0_and_stale_strings_are_gone(self) -> None:
        self.assertEqual(nightshift.__version__, "0.2.0")
        with tempfile.TemporaryDirectory() as raw:
            job_dir = Path(raw)
            (job_dir / "prompt.md").write_text("look\n", encoding="utf-8")
            (job_dir / "job.toml").write_text(
                'schema_version = 1\nid = "cursor-job"\ndescription = "no"\ntype = "note"\n'
                'repository = "/tmp/x"\nbase_ref = "HEAD"\nprovider = "cursor"\n'
                "max_runtime_seconds = 30\nmax_attempts = 1\nconcurrency_group = \"c\"\n"
                "network = false\nwrite_scope = \"none\"\nexpected_artifacts = []\n"
                "verification = []\nsuccess_criteria = [\"x\"]\n",
                encoding="utf-8",
            )
            with self.assertRaises(JobValidationError) as caught:
                load_job(job_dir)
            self.assertIn("not implemented", str(caught.exception))
            self.assertNotIn("v0.1", str(caught.exception))
        from nightshift.models import Job

        job = Job.from_dict(
            {
                "schema_version": 1,
                "id": "wording",
                "description": "wording",
                "type": "note",
                "repository": "/tmp/x",
                "base_ref": "HEAD",
                "provider": "fake",
                "model": None,
                "max_runtime_seconds": 30,
                "max_attempts": 1,
                "concurrency_group": "wording",
                "network": False,
                "write_scope": "workspace",
                "expected_artifacts": [],
                "verification": [],
                "success_criteria": ["x"],
                "allow_bash": [],
                "prompt": "x\n",
                "job_dir": "/tmp",
                "job_file": "/tmp/job.toml",
                "isolation": "clone",
                "allow_legacy_shell": False,
                "verification_steps": [],
            }
        )
        preamble = render_preamble(job)
        self.assertIn("Local commits in this workspace are allowed", preamble)
        self.assertNotIn("this worktree", preamble)
        self.assertNotIn("--sandbox", " ".join(build_policy(job).allow))


class ReadOnlyManifestTests(unittest.TestCase):
    def test_read_only_real_job_shape_validates_without_launching_grok(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            make_repo(source)
            job_dir = root / "job"
            write_job(
                job_dir,
                source,
                id="self-audit",
                provider="grok",
                isolation="clone",
                write_scope="none",
                network=False,
                max_attempts=1,
                max_runtime_seconds=900,
                verification=[],
                expected_artifacts=[],
                prompt="Perform a read-only architecture and correctness audit.\n",
            )
            previous = os.environ.get("NIGHTSHIFT_FORBID_GROK")
            os.environ["NIGHTSHIFT_FORBID_GROK"] = "1"
            try:
                job = load_job(job_dir)
                self.assertEqual(job.provider, "grok")
                self.assertEqual(job.isolation, "clone")
                self.assertEqual(job.write_scope, "none")
                self.assertFalse(job.network)
                self.assertEqual(job.max_attempts, 1)
                config = load_config(root / "ns")
                config.ensure_dirs()
                db = Database(config.db_path)
                locks = LockManager(db)
                try:
                    run = enqueue(db, job_dir, provider="grok")
                    finished = execute_run(config, db, locks, run.run_id)
                finally:
                    db.close()
            finally:
                if previous is None:
                    os.environ.pop("NIGHTSHIFT_FORBID_GROK", None)
                else:
                    os.environ["NIGHTSHIFT_FORBID_GROK"] = previous
            self.assertEqual(finished.state, RunState.BLOCKED.value)
            self.assertIn("NIGHTSHIFT_FORBID_GROK", finished.failure_reason)


if __name__ == "__main__":
    unittest.main()
