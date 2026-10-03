import tempfile
import unittest
from pathlib import Path

from nightshift.config import load_config
from nightshift.job import JobValidationError, load_job
from nightshift.models import UsageError
from nightshift.testkit import make_repo, write_job


class InvalidInputTests(unittest.TestCase):
    def test_unhashable_enums_and_boolean_version_are_structured_job_errors(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            job = write_job(root, root / 'source') / 'job.toml'
            original = job.read_text()
            for before, after in [('schema_version = 1', 'schema_version = true'),
                                  ('provider = "fake"', 'provider = []'),
                                  ('write_scope = "workspace"', 'write_scope = {}')]:
                job.write_text(original.replace(before, after))
                with self.subTest(after=after), self.assertRaises(JobValidationError):
                    load_job(job)

    def test_invalid_config_types_unknown_fields_and_overlapping_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = root / 'nightshift.toml'
            for text in ('[supervisor]\nconcurrency = true\n', '[provider]\nmax_turns = -1\n',
                         '[policy]\ndestroy_failed_worktrees = "false"\n',
                         '[provider]\npermission_mode = "default"\n',
                         '[paths]\nstate_dir="."\n', '[supervisor]\nconcurency=9\n',
                         '[supervisor]\npoll_interval_seconds=nan\n', 'supervisor=[]\n'):
                config.write_text(text)
                with self.subTest(text=text), self.assertRaises(UsageError):
                    load_config(root, config)

    def test_read_roots_reject_blank_non_array_and_control_directory_overlap(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = root / 'nightshift.toml'
            for value in ('[""]', '[" "]', '""', '["."]', '["state"]', '["state/credentials"]', '["/"]'):
                config.write_text('[containment]\nread_roots=' + value + '\n')
                with self.subTest(value=value), self.assertRaises(UsageError):
                    load_config(root, config)

    def test_intake_refuses_recursive_control_plane_targets(self):
        from nightshift.intake import plan_order
        from nightshift.work_order import WorkOrderError
        from test_work_order_intake import local_order
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            order = local_order(root)
            source = Path(order['repository']['path'])
            (source / 'pyproject.toml').write_text('[project]\nname="nightshift"\n')
            (source / 'src/nightshift').mkdir(parents=True)
            (source / 'src/nightshift/__init__.py').write_text('')
            with self.assertRaisesRegex(WorkOrderError, 'control plane'):
                plan_order(order, provider='fake')
