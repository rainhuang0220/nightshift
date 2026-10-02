# Contributing

Issues and pull requests are welcome.

Nightshift is a small local control plane. A change should stay inspectable: one behavior, a test when the behavior changes, and a coherent commit.

## Tests

Behavioral changes need a test. Safety changes need a regression that fails before the fix and passes after it.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Use `discover -s tests`. Importing `tests.<module>` can load a different top-level package named `tests`.

Tests use `FakeProvider` and temporary Git repositories. A test that launches Grok, pushes, or contacts a network is a broken test. Set `NIGHTSHIFT_FORBID_GROK=1` in any new test process.

`nightshift safety probe` is the deterministic probe. `nightshift safety probe --provider grok` is a sacrificial canary on a temporary repository. Do not point it at a repository you need, and do not put that canary in ordinary CI.

## Reports and logs

Do not paste credentials, tokens, auth files, or private session contents into issues, pull requests, or logs. If a report needs a path, use a placeholder.

## Commits

Keep commits coherent. The author and committer of record are the person who owns the change.

Contributor sign-off is not required. The project does not use a Developer Certificate of Origin unless it adopts one later.

## Versions

`main` is the primary branch. Release tags follow semantic versioning:

- a patch release fixes a bug or a safety defect without adding a feature
- a minor release adds a backwards-compatible capability
- a major release changes the job format, config, or runtime in an incompatible way

Experimental work stays under `[Unreleased]` in `CHANGELOG.md` until that milestone passes its tests and its safety gate.
