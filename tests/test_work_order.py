import copy
import json
import tempfile
import unittest
from pathlib import Path


def sample_order():
    return {
        'schema': 'engineering.work-order', 'version': 1, 'task_id': 'task-1',
        'origin': {'name': 'independent-planner', 'reference': 'opportunity-1'},
        'repository': {'identity': 'acme/project', 'path': '/tmp/project', 'url': None,
                       'base_revision': 'a' * 40},
        'title': 'Fix parser', 'objective': 'Reject invalid input', 'rationale': 'Observed crash',
        'evidence': [{'id': 'obs-1', 'kind': 'observation', 'summary': 'Parser crashes',
                      'source': 'local:test-output', 'observed_at': '2026-10-03T00:00:00Z',
                      'expires_at': '2026-10-06T00:00:00Z', 'observation_ids': ['obs-1']}],
        'constraints': ['Keep the public API'],
        'validation': [{'argv': ['python3', '-m', 'unittest'], 'expectation': 'Tests pass', 'timeout_seconds': 60}],
        'actions': {'allowed': ['read', 'edit', 'commit'],
                    'forbidden': ['push', 'publish', 'deploy', 'credentials', 'external-write']},
        'references': [], 'created_at': '2026-10-03T00:00:00Z',
        'provenance': {'opportunity_id': 'opportunity-1', 'decision_at': '2026-10-03T00:00:00Z',
                       'evidence_ids': ['obs-1']},
    }


class WorkOrderContractTests(unittest.TestCase):
    def test_round_trip_is_canonical_and_independent(self):
        from nightshift.work_order import dumps, loads
        data = sample_order()
        rendered = dumps(data)
        reordered = dict(reversed(list(data.items())))
        self.assertEqual(rendered, dumps(reordered))
        self.assertEqual(loads(rendered), data)
        self.assertTrue(rendered.endswith('\n'))

    def test_bad_versions_shapes_lineage_and_unknown_fields_are_rejected(self):
        from nightshift.work_order import WorkOrderError, validate
        for key, value in [('version', 2), ('version', True), ('title', ''),
                           ('evidence', []), ('validation', []), ('actions', []),
                           ('created_at', '2026-10-03'), ('unexpected', 'x')]:
            data = sample_order()
            data[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(WorkOrderError):
                validate(data)
        data = sample_order()
        data['provenance']['evidence_ids'] = ['invented']
        with self.assertRaises(WorkOrderError):
            validate(data)

    def test_duplicate_keys_nonfinite_json_and_large_inputs_are_rejected(self):
        from nightshift.work_order import WorkOrderError, loads
        for text in ['{"version":1,"version":2}', '{"value":NaN}', '[' * 1100 + ']' * 1100, 'x' * 1048577]:
            with self.assertRaises(WorkOrderError):
                loads(text)

    def test_paths_permissions_argv_and_temporal_provenance_fail_closed(self):
        from nightshift.work_order import WorkOrderError, validate
        mutations = [
            lambda x: x['repository'].update(path='../repo'),
            lambda x: x['repository'].update(base_revision='HEAD'),
            lambda x: x['actions']['allowed'].append('push'),
            lambda x: x['actions']['forbidden'].append('edit'),
            lambda x: x['validation'][0].update(argv=['python3', '\x00']),
            lambda x: x['validation'][0].update(timeout_seconds=True),
            lambda x: x['evidence'][0].update(observed_at='2026-10-07T00:00:00Z'),
            lambda x: x['evidence'][0].update(kind='inference'),
        ]
        for mutation in mutations:
            data = copy.deepcopy(sample_order())
            mutation(data)
            with self.assertRaises(WorkOrderError):
                validate(data)
