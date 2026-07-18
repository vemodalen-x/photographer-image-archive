# Contributing

Use Python 3.10 or newer on Windows.

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check .
python -m compileall -q photo_archive_app.py photo_archive_cli.py photo_archive_core.py photo_archive_version.py tools tests
```

Do not commit downloaded photographs, archive databases, screenshots containing local data, browser profiles, logs, credentials, or absolute developer paths.
