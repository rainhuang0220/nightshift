"""Nightshift v0.2 isolation and safety-gate tests. No test calls Grok."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from nightshift.config import load_config
from nightshift.containment import contained_run, write_profile
from nightshift.db import Database
from nightshift.extensions import audit_payload, neutralize_project_extensions
from nightshift.finalize import SOURCE_INTEGRITY_VIOLATION, decide_final
from nightshift.integrity import capture, compare
from nightshift.job import JobValidationError, load_job
from nightshift.locks import LockManager
from nightshift.models import RunState
from nightshift.probe import run_fake_probe, run_safety_probe
from nightshift.providers.grok import build_grok_argv
from nightshift.policy import build_policy, minimal_env
from nightshift.queue import enqueue
from nightshift.runner import _child_env, execute_run
from nightshift.runtime import auth_bootstrap, auth_status
from nightshift.testkit import git, make_repo, write_job
from nightshift.workspace import WorktreeWorkspaceBackend, inspect_source, prepare_workspace


class CloneTests(unittest.TestCase):
    def test_clone_is_independent_of_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            dest = root / "clone"
            make_repo(source, dirty=True)
            (source / "keep.txt").write_text("keep\n", encoding="utf-8")
            git(source, "add", "keep.txt")
            git(source, "commit", "-m", "keep")
            (source / "DIRTY.txt").write_text("dirty\n", encoding="utf-8")
            before = capture(source)
            head = before.head
            refs = before.refs
            prepare_workspace(inspect_source(source, "HEAD"), dest, "clone")
            after = compare(before, capture(source))
            self.assertTrue(after.ok, after.changed_categories)
            self.assertFalse((dest / "DIRTY.txt").exists())
            self.assertEqual(git(dest, "rev-parse", "HEAD").stdout.strip(), head)
            self.assertEqual(git(dest, "remote").stdout.strip(), "")
            self.assertFalse((source / ".git" / "worktrees").exists())
            git(dest, "commit", "--allow-empty", "-m", "only in clone")
            self.assertEqual(git(source, "rev-parse", "HEAD").stdout.strip(), head)
            self.assertEqual(capture(source).refs, refs)
            self.assertNotEqual(git(dest, "rev-parse", "HEAD").stdout.strip(), head)


class IntegrityTests(unittest.TestCase):
    def test_each_claimed_category_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            make_repo(repo)
            base = capture(repo)
            (repo / "README.md").write_text("changed\n", encoding="utf-8")
            self.assertIn("porcelain", compare(base, capture(repo)).changed_categories)
            git(repo, "add", "README.md")
            self.assertIn("index", compare(base, capture(repo)).changed_categories)
            git(repo, "commit", "-m", "move")
            moved = compare(base, capture(repo)).changed_categories
            self.assertIn("head", moved)
            self.assertIn("refs", moved)
            base = capture(repo)
            git(repo, "update-ref", "refs/heads/other", "HEAD")
            self.assertIn("refs", compare(base, capture(repo)).changed_categories)
            git(repo, "update-ref", "-d", "refs/heads/other")
            git(repo, "config", "--local", "nightshift.canary", "1")
            self.assertEqual(compare(base, capture(repo)).changed_categories, ("config",))
            git(repo, "config", "--local", "--unset", "nightshift.canary")
            hook = repo / ".git" / "hooks" / "nightshift-canary"
            hook.write_text("#!/bin/sh\n", encoding="utf-8")
            self.assertIn("hooks", compare(base, capture(repo)).changed_categories)
            hook.unlink()
            other = root / "linked"
            from nightshift.workspace import create_worktree

            create_worktree(inspect_source(repo, "HEAD"), other)
            self.assertIn("worktrees", compare(base, capture(repo)).changed_categories)

    def test_finalization_gate_rejects_a_missing_baseline(self) -> None:
        decision = decide_final(
            provider_exit_code=0,
            verification_ran=True,
            verification_exit_code=0,
            workspace_exists=True,
            integrity_ok=False,
            safety_audit_ok=True,
        )
        self.assertNotEqual(decision.state, RunState.SUCCEEDED.value)
        self.assertEqual(decision.reason, SOURCE_INTEGRITY_VIOLATION)


class RuntimeTests(unittest.TestCase):
    def test_child_env_uses_isolated_homes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = load_config(root)
            config.ensure_dirs()
            env = _child_env(config, root / "run", root / "workspace", root / "source", "ns-test")
            self.assertNotEqual(Path(env["HOME"]).resolve(), Path.home().resolve())
            self.assertEqual(Path(env["GROK_HOME"]).resolve(), (root / "run" / "grok-home").resolve())
            self.assertNotEqual(Path(env["GROK_HOME"]).resolve(), config.auth_store().resolve())
            self.assertEqual(env["GROK_MEMORY"], "0")
            self.assertEqual(env["GROK_WORKFLOWS"], "0")
            self.assertEqual(stat.S_IMODE(Path(env["HOME"]).stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(Path(env["GROK_HOME"]).stat().st_mode), 0o700)
            self.assertNotIn("SSH_AUTH_SOCK", env)
            self.assertNotIn("GITHUB_TOKEN", env)

    def test_auth_bootstrap_copies_only_auth_file_and_locks_it_down(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            operator = root / "operator"
            operator.mkdir()
            (operator / "auth.json").write_bytes(b"synthetic-auth-material")
            (operator / "config.toml").write_text("secret = true\n", encoding="utf-8")
            (operator / "mcp_credentials.json").write_text("{}\n", encoding="utf-8")
            profile = root / "profile"
            message = auth_bootstrap(profile, source_home=operator)
            self.assertEqual(message, "bootstrapped")
            self.assertEqual((profile / "auth.json").read_bytes(), b"synthetic-auth-material")
            self.assertFalse((profile / "config.toml").exists())
            self.assertFalse((profile / "mcp_credentials.json").exists())
            self.assertEqual(stat.S_IMODE((profile / "auth.json").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(profile.stat().st_mode), 0o700)
            status = auth_status(profile)
            self.assertEqual(status["auth"], "present")
            self.assertNotIn(b"synthetic-auth-material", str(status).encode())

    def test_minimal_env_drops_provider_prefixes(self) -> None:
        env = minimal_env(
            {"PATH": "/usr/bin", "HOME": "/tmp/home", "AWS_PROFILE": "dev", "KAGGLE_USERNAME": "nope", "LANG": "C"},
            extra={"GH_ENTERPRISE_HOST": "nope"},
        )
        self.assertNotIn("AWS_PROFILE", env)
        self.assertNotIn("KAGGLE_USERNAME", env)
        self.assertNotIn("GH_ENTERPRISE_HOST", env)
        self.assertEqual(env["HOME"], "/tmp/home")


class ExtensionTests(unittest.TestCase):
    def test_operator_surface_is_rejected_and_builtin_agents_remain(self) -> None:
        audit = audit_payload(
            {
                "hooks": [
                    {"source": {"type": "user"}, "vendor": "claude"},
                    {"source": {"type": "plugin"}},
                ],
                "skills": [{"source": {"type": "plugin", "plugin_name": "demo"}}],
                "plugins": [{"scope": "project", "name": "demo"}],
                "mcpServers": [{"source": {"type": "plugin", "vendor": "cursor"}}],
                "agents": [{"name": "plan", "source": {"type": "builtin"}}],
                "projectInstructions": [{"scope": "project", "fileType": "agents_md"}],
                "permissions": {"sources": [], "loaded": 2},
                "lspServers": [],
                "marketplaces": [],
            },
            grok_home=Path("/tmp/nightshift-profile"),
        )
        self.assertFalse(audit.ok)
        self.assertTrue(any("hook" in item for item in audit.violations))
        self.assertTrue(any("plugin" in item for item in audit.violations))
        self.assertTrue(any("mcp" in item for item in audit.violations))
        self.assertTrue(any("permission" in item for item in audit.violations))
        self.assertEqual(audit.untrusted_instructions, 1)
        self.assertNotIn("builtin agent", audit.violations)

    def test_project_extensions_are_neutralized_only_in_the_clone(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            dest = root / "clone"
            make_repo(source)
            (source / ".grok").mkdir()
            (source / ".grok" / "config.toml").write_text("[mcp_servers.fake]\ncommand='/usr/bin/true'\n", encoding="utf-8")
            (source / ".mcp.json").write_text("{}\n", encoding="utf-8")
            (source / "AGENTS.md").write_text("untrusted\n", encoding="utf-8")
            git(source, "add", "-A")
            git(source, "commit", "-m", "extensions")
            prepare_workspace(inspect_source(source, "HEAD"), dest, "clone")
            moved = neutralize_project_extensions(dest, root / "record")
            self.assertTrue(any(item.startswith(".grok") for item in moved))
            self.assertIn(".mcp.json", moved)
            self.assertFalse((dest / ".grok").exists())
            self.assertFalse((dest / ".mcp.json").exists())
            self.assertTrue((dest / "AGENTS.md").is_file())
            self.assertTrue((source / ".grok" / "config.toml").is_file())
            self.assertTrue((source / ".mcp.json").is_file())


class VerificationTests(unittest.TestCase):
    def test_structured_verification_parses_and_legacy_shell_blocks_grok(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            make_repo(source)
            job_dir = root / "job"
            job_dir.mkdir()
            (job_dir / "prompt.md").write_text("probe\n", encoding="utf-8")
            (job_dir / "job.toml").write_text(
                "\n".join(
                    [
                        "schema_version = 1",
                        'id = "structured"',
                        'description = "structured verification"',
                        'type = "note"',
                        f'repository = "{source}"',
                        'base_ref = "HEAD"',
                        'provider = "grok"',
                        "max_runtime_seconds = 30",
                        "max_attempts = 1",
                        'concurrency_group = "structured"',
                        "network = false",
                        'write_scope = "none"',
                        "expected_artifacts = []",
                        'success_criteria = ["recorded"]',
                        "[[verification]]",
                        'argv = ["python3", "-c", "print(1)"]',
                        "timeout_seconds = 30",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            job = load_job(job_dir)
            self.assertEqual(job.verification_steps[0]["argv"][:2], ["python3", "-c"])
            self.assertFalse(job.verification_steps[0]["shell"])
            self.assertEqual(job.isolation, "clone")
            write_job(root / "legacy", source, provider="grok", verification=["echo unsafe"])
            config = load_config(root / "ns")
            config.ensure_dirs()
            db = Database(config.db_path)
            locks = LockManager(db)
            previous = os.environ.get("NIGHTSHIFT_FORBID_GROK")
            os.environ["NIGHTSHIFT_FORBID_GROK"] = "1"
            try:
                run = enqueue(db, root / "legacy", provider="grok")
                finished = execute_run(config, db, locks, run.run_id)
            finally:
                if previous is None:
                    os.environ.pop("NIGHTSHIFT_FORBID_GROK", None)
                else:
                    os.environ["NIGHTSHIFT_FORBID_GROK"] = previous
                db.close()
            self.assertEqual(finished.state, RunState.BLOCKED.value)
            self.assertIn("legacy shell", finished.failure_reason)

    def test_mixed_verification_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            job = Path(raw) / "job.toml"
            job.write_text(
                'schema_version = 1\nid = "bad"\ndescription = "bad"\ntype = "note"\n'
                'repository = "/tmp/x"\nbase_ref = "HEAD"\nprovider = "fake"\n'
                "max_runtime_seconds = 30\nmax_attempts = 1\nconcurrency_group = \"bad\"\n"
                "network = false\nwrite_scope = \"none\"\nexpected_artifacts = []\n"
                'verification = ["echo hi"]\nsuccess_criteria = ["x"]\n'
                "[[verification]]\nargv = [\"python3\"]\n",
                encoding="utf-8",
            )
            (Path(raw) / "prompt.md").write_text("x\n", encoding="utf-8")
            with self.assertRaises(JobValidationError):
                load_job(Path(raw))


class SeatbeltTests(unittest.TestCase):
    def test_absolute_interpreters_cannot_write_outside_or_push(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            workspace = root / "workspace"
            bare = root / "bare.git"
            sentinel = root / "sentinel.txt"
            make_repo(source)
            (source / "secret.txt").write_text("keep\n", encoding="utf-8")
            git(source, "add", "secret.txt")
            git(source, "commit", "-m", "secret")
            prepare_workspace(inspect_source(source, "HEAD"), workspace, "clone")
            subprocess.check_call(["git", "init", "--bare", str(bare)], stdout=subprocess.DEVNULL)
            sentinel.write_text("stay\n", encoding="utf-8")
            runtime = root / "run"
            runtime.mkdir()
            profile = write_profile(runtime / "probe.sb", writable=[workspace, runtime], network=False)
            env = {"PATH": "/usr/bin:/bin", "HOME": str(runtime), "TMPDIR": str(runtime)}
            code, _output = contained_run(
                ["/usr/bin/python3", "-c", "open('inside.txt','w').write('ok')"],
                cwd=workspace,
                env=env,
                profile=profile,
                timeout=30,
            )
            self.assertEqual(code, 0)
            self.assertEqual((workspace / "inside.txt").read_text(encoding="utf-8"), "ok")
            contained_run(
                ["/usr/bin/python3", "-c", f"open({str(source / 'secret.txt')!r},'a').write('no')"],
                cwd=workspace,
                env=env,
                profile=profile,
                timeout=30,
            )
            self.assertEqual((source / "secret.txt").read_text(encoding="utf-8"), "keep\n")
            contained_run(
                ["/bin/sh", "-c", f"echo no >> {sentinel}"],
                cwd=workspace,
                env=env,
                profile=profile,
                timeout=30,
            )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "stay\n")
            contained_run(["/usr/bin/git", "add", "inside.txt"], cwd=workspace, env=env, profile=profile, timeout=30)
            commit, _output = contained_run(
                ["/usr/bin/git", "-c", "user.email=nightshift@localhost", "-c", "user.name=Nightshift", "commit", "-m", "inside"],
                cwd=workspace,
                env=env,
                profile=profile,
                timeout=30,
            )
            self.assertEqual(commit, 0, _output)
            for argv in (
                ["/usr/bin/git", "-C", str(workspace), "push", str(bare), "HEAD:refs/heads/probe"],
                [
                    "/usr/bin/python3",
                    "-c",
                    "import subprocess,sys; sys.exit(subprocess.run(['/usr/bin/git','-C',"
                    f"{str(workspace)!r},'push',{str(bare)!r},'HEAD:refs/heads/probe']).returncode)",
                ],
            ):
                contained_run(argv, cwd=workspace, env=env, profile=profile, timeout=30)
            show = subprocess.run(["git", "--git-dir", str(bare), "show-ref"], check=False, capture_output=True, text=True)
            self.assertEqual(show.stdout.strip(), "")
            self.assertEqual(capture(source).head, git(source, "rev-parse", "HEAD").stdout.strip())


class ProbeTests(unittest.TestCase):
    def test_fake_probe_passes_without_calling_grok(self) -> None:
        code, text = run_safety_probe("fake")
        self.assertEqual(code, 0, text)
        self.assertTrue(text.startswith("PASS"))
        self.assertIn("grok: not called", text)
        self.assertIn("HARD BLOCK", text)
        self.assertIn(
            "ACTION: PATH git push to local bare\nEXPECTED: blocked\nACTUAL: blocked\nENFORCEMENT LAYER: DEFENSE IN DEPTH",
            text,
        )

    def test_unchanged_postcondition_is_not_a_hard_block(self) -> None:
        from nightshift.probe import ADVISORY_ONLY, _grok_effects

        root = Path(tempfile.mkdtemp(prefix="nightshift-post-"))
        try:
            source = root / "source"
            source.mkdir()
            (source / "README.md").write_text("probe\n", encoding="utf-8")
            sentinel = root / "sentinel.txt"
            sentinel.write_text("stay\n", encoding="utf-8")
            bare = root / "bare.git"
            subprocess.check_call(["git", "init", "--bare", str(bare)], stdout=subprocess.DEVNULL)
            rows = _grok_effects(root / "missing", source, sentinel, bare)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        blocked = [row for row in rows if row.expected == "blocked"]
        self.assertTrue(blocked)
        for row in blocked:
            self.assertEqual(row.layer, ADVISORY_ONLY, row.render())
            self.assertEqual(row.actual, "blocked")

    def test_worktree_backend_is_labeled_weaker(self) -> None:
        text = WorktreeWorkspaceBackend().import_instructions()
        self.assertIn("WEAKER ISOLATION", text)
        self.assertIn("SHARES SOURCE GIT METADATA", text)
        self.assertIn("NOT DEFAULT", text)

    def test_grok_argv_disables_ambient_features(self) -> None:
        from nightshift.models import Job

        job = Job(
            schema_version=1,
            id="argv",
            description="argv",
            type="note",
            repository="/tmp/source",
            base_ref="HEAD",
            provider="grok",
            model=None,
            max_runtime_seconds=30,
            max_attempts=1,
            concurrency_group="argv",
            network=False,
            write_scope="workspace",
            expected_artifacts=[],
            verification=[],
            success_criteria=["done"],
            allow_bash=[],
            prompt="x",
            job_dir="/tmp",
            job_file="/tmp/job.toml",
        )
        argv = build_grok_argv(
            binary="grok",
            cwd=Path("/tmp/workspace"),
            prompt_file=Path("/tmp/prompt.md"),
            session_id="11111111-1111-1111-1111-111111111111",
            model=None,
            max_turns=4,
            policy=build_policy(job),
        )
        self.assertIn("--permission-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("--no-memory", argv)
        self.assertIn("--no-subagents", argv)
        self.assertIn("--disable-web-search", argv)
        self.assertNotIn("--sandbox", argv)
        self.assertNotIn("bypassPermissions", argv)

    def test_real_gate_fails_when_the_provider_did_not_exit_zero(self) -> None:
        from types import SimpleNamespace

        from nightshift.probe import _real_gate

        root = Path(tempfile.mkdtemp(prefix="nightshift-gate-"))
        try:
            source = root / "source"
            source.mkdir()
            (source / "README.md").write_text("probe\n", encoding="utf-8")
            sentinel = root / "sentinel.txt"
            sentinel.write_text("stay\n", encoding="utf-8")
            delta = SimpleNamespace(ok=True, changed_categories=())
            failed = _real_gate(delta, [], [], True, sentinel, source, provider_exit_code=1)
            ok = _real_gate(delta, [], [], True, sentinel, source, provider_exit_code=0)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        self.assertEqual(failed, "FAIL")
        self.assertEqual(ok, "PASS_WITH_LIMITATIONS")

    def test_streaming_json_text_events_are_summarized(self) -> None:
        from nightshift.report import summarize_stream

        raw = '{"type":"text","data":"pong"}\n{"type":"usage","usage":{"output_tokens":1}}\n'
        self.assertEqual(summarize_stream(raw), "pong")


class LogTests(unittest.TestCase):
    def test_fake_run_log_modes_and_source_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            make_repo(source, dirty=True)
            before = capture(source)
            job = write_job(root / "job", source)
            config = load_config(root / "ns")
            config.ensure_dirs()
            db = Database(config.db_path)
            locks = LockManager(db)
            previous = os.environ.get("NIGHTSHIFT_FORBID_GROK")
            os.environ["NIGHTSHIFT_FORBID_GROK"] = "1"
            try:
                run = enqueue(db, job, provider="fake")
                finished = execute_run(config, db, locks, run.run_id)
            finally:
                if previous is None:
                    os.environ.pop("NIGHTSHIFT_FORBID_GROK", None)
                else:
                    os.environ["NIGHTSHIFT_FORBID_GROK"] = previous
                db.close()
            self.assertEqual(finished.state, RunState.SUCCEEDED.value, finished.failure_reason)
            self.assertTrue(compare(before, capture(source)).ok)
            run_dir = Path(finished.run_dir)
            self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)
            for name in ("provider.stdout.log", "provider.stderr.log", "verification.log", "report.md"):
                self.assertEqual(stat.S_IMODE((run_dir / name).stat().st_mode) & 0o077, 0)
            self.assertIn("nightshift: record fake provider note", (run_dir / "report.md").read_text(encoding="utf-8"))
            report = (run_dir / "report.md").read_text(encoding="utf-8")
            self.assertIn("does not merge or push", report)


if __name__ == "__main__":
    unittest.main()
