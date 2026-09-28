# Contributing

Thanks for helping! Issues and pull requests in **Polish or English** are both welcome.

## Reporting a problem

Use the [bug report form](https://github.com/MichalZaniewicz/ha-librus-synergia/issues/new/choose). A **diagnostics file** (Settings → Devices & services → Librus Synergia → ⋮ → Download diagnostics) answers most questions up front; password, login and cookies are redacted in it automatically. Please remove children's names, grades and message text before posting anything else.

Missing or wrong **Librus data** usually starts in the client library, [librus-synergia](https://github.com/MichalZaniewicz/librus-synergia). Report it here anyway, and it'll be moved if needed.

## Where the code lives

- **Talking to Librus** (login, endpoints, JSON parsing) lives in the [librus-synergia](https://github.com/MichalZaniewicz/librus-synergia) library on PyPI, not here. Changes there get released first, then pinned in `manifest.json`.
- **This repo** holds the Home Assistant side: `coordinator.py` (polling, events, degrading gracefully), `sensor.py`, `calendar.py`, `config_flow.py`, `services.py`, and the blueprints.

## Development

```bash
pip install -r requirements_test.txt   # needs Python 3.14
pytest tests/
```

The Home Assistant test harness needs Linux or macOS. It doesn't run on Windows, so rely on CI (or WSL) there.

## Pull requests

- Keep a PR to one change, and add or update a test for it.
- Update `CHANGELOG.md` and translations (`strings.json`, `translations/en.json`, `translations/pl.json`) when user-facing text changes.
- **Never commit real credentials, cookies or real children's data**, including in test fixtures.
- Test against a real account with **normal** logins only. Don't script wrong passwords or rapid retries: it's a real family's school account and Librus may have abuse protection.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
