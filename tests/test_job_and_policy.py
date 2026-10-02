import tempfile
import unittest
from pathlib import Path

from nightshift.job import JobValidationError, load_job
from nightshift.models import Job
from nightshift.policy import DENY_RULES, build_policy, minimal_env, render_preamble
from nightshift.providers.grok import build_grok_argv
from nightshift.testkit import ROOT, write_job


class JobTests(unittest.TestCase):
    def test_example_job_validates(self) -> None:
        job = load_job(ROOT / "jobs" / "examples" / "repo_audit")
        self.assertEqual(job.id, "repo-audit-example")
        self.assertEqual(job.provider, "fake")
        self.assertFalse(job.network)
        self.assertEqual(job.write_scope, "none")
        self.assertTrue(job.prompt.strip())

    def test_missing_required_field_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            target = directory / "job.toml"
            target.write_text(
                'schema_version = 1\ndescription = "x"\n',
                encoding="utf-8",
            )
            (directory / "prompt.md").write_text("hello\n", encoding="utf-8")
            with self.assertRaises(JobValidationError) as caught:
                load_job(directory)
            self.assertTrue(any("id" in error for error in caught.exception.errors))

    def test_forbidden_allow_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            write_job(directory, Path("/tmp/unused"), allow_bash=["git push*"])
            with self.assertRaises(JobValidationError) as caught:
                load_job(directory)
            self.assertTrue(any("allow_bash" in error for error in caught.exception.errors))


class PolicyTests(unittest.TestCase):
    def _job(self, **changes) -> Job:
        data = dict(
            schema_version=1,
            id="policy-job",
            description="policy",
            type="note",
            repository="/work/source",
            base_ref="HEAD",
            provider="grok",
            model=None,
            max_runtime_seconds=60,
            max_attempts=1,
            concurrency_group="g",
            network=False,
            write_scope="workspace",
            expected_artifacts=[],
            verification=[],
            success_criteria=["done"],
            allow_bash=[],
            prompt="do the work\n",
            job_dir="/work",
            job_file="/work/job.toml",
        )
        data.update(changes)
        return Job(**data)

    def test_default_denies_cover_the_overnight_policy(self) -> None:
        blob = "\n".join(DENY_RULES)
        for snippet in (
            "git push",
            "gh pr create",
            "gh pr merge",
            "kaggle",
            "npm publish",
            "sudo",
            "rm -rf",
            "git config --global",
            "git clean",
            "git reset --hard",
        ):
            self.assertIn(snippet, blob)

    def test_preamble_is_marked_advisory(self) -> None:
        text = render_preamble(self._job())
        self.assertIn("not a security boundary", text)
        self.assertIn("git push", text)

    def test_offline_workspace_policy_disables_web_search(self) -> None:
        policy = build_policy(self._job(network=False, write_scope="workspace"))
        self.assertEqual(policy.permission_mode, "dontAsk")
        self.assertEqual(policy.sandbox_profile, "workspace")
        self.assertTrue(policy.disable_web_search)
        self.assertIn("Edit", policy.allow)

    def test_read_only_policy_uses_read_only_sandbox(self) -> None:
        policy = build_policy(self._job(write_scope="none", network=True))
        self.assertEqual(policy.sandbox_profile, "read-only")
        self.assertNotIn("Edit", policy.allow)
        self.assertFalse(policy.disable_web_search)
        self.assertIn("WebSearch", policy.allow)

    def test_grok_argv_is_unattended_but_not_yolo(self) -> None:
        job = self._job()
        policy = build_policy(job)
        argv = build_grok_argv(
            binary="grok",
            cwd=Path("/work/workspace"),
            prompt_file=Path("/work/prompt.final.md"),
            session_id="11111111-1111-1111-1111-111111111111",
            model="grok-test",
            max_turns=12,
            policy=policy,
        )
        self.assertEqual(argv[0], "grok")
        self.assertIn("--cwd", argv)
        self.assertEqual(argv[argv.index("--cwd") + 1], "/work/workspace")
        self.assertIn("--prompt-file", argv)
        self.assertIn("--deny", argv)
        self.assertNotIn("--sandbox", argv)
        self.assertEqual(policy.sandbox_profile, "workspace")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("--disable-web-search", argv)
        self.assertIn("--session-id", argv)
        self.assertIn("--no-subagents", argv)
        self.assertIn("--no-memory", argv)
        self.assertIn("--no-auto-update", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "streaming-json")
        self.assertNotIn("--always-approve", argv)
        self.assertNotIn("bypassPermissions", argv)
        self.assertNotIn("--worktree", argv)
        self.assertNotIn("headless", argv)
        self.assertNotIn("agent", argv)
        joined = " ".join(argv)
        self.assertIn("Bash(git push*)", joined)

    def test_resume_argv_uses_resume_and_keeps_denies(self) -> None:
        policy = build_policy(self._job())
        argv = build_grok_argv(
            binary="grok",
            cwd=Path("/work/workspace"),
            prompt_file=Path("/work/prompt.final.md"),
            session_id="11111111-1111-1111-1111-111111111111",
            model=None,
            max_turns=4,
            policy=policy,
            resume=True,
        )
        self.assertIn("--resume", argv)
        self.assertNotIn("--session-id", argv)
        self.assertNotIn("--always-approve", argv)
        self.assertIn("--deny", argv)

    def test_minimal_env_drops_secrets_and_keeps_path(self) -> None:
        env = minimal_env(
            {
                "PATH": "/usr/bin",
                "HOME": "/home/operator",
                "AWS_SECRET_ACCESS_KEY": "hidden",
                "GITHUB_TOKEN": "hidden",
                "LANG": "C",
            },
            extra={"NIGHTSHIFT_FAKE_RESULT": "success", "API_TOKEN": "nope"},
        )
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertIn("HOME", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("API_TOKEN", env)
        self.assertEqual(env["NIGHTSHIFT_FAKE_RESULT"], "success")

    def test_yolo_permission_mode_is_refused(self) -> None:
        with self.assertRaises(Exception):
            build_policy(self._job(), permission_mode="bypassPermissions")


if __name__ == "__main__":
    unittest.main()
