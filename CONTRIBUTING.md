# Contributing

Use Python 3.10 or newer on Windows.

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check .
python -m compileall -q photo_archive_app.py photo_archive_cli.py photo_archive_core.py photo_archive_version.py tools tests
```

Release and CI environments install `requirements-release.lock` with `--require-hashes`. Regenerate the lock only as an intentional dependency update and review the complete diff.

Do not commit downloaded photographs, archive databases, screenshots containing local data, browser profiles, logs, credentials, or absolute developer paths.
