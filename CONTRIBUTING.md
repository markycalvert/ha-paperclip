# Contributing

Contributions are welcome: bug reports, ideas, documentation and code.

## Ground rules

- Open an issue before a large change so we can agree on the approach.
- Keep pull requests focused on one thing.
- Every user-facing string goes in `custom_components/paperclip/translations/en.json`.
- Never commit real hostnames, IP addresses, API keys or personal data. Use the
  neutral examples the project already uses: `http://paperclip.local:3100` or
  `http://192.0.2.10:3100`, company "Acme", agents "CEO", "Engineer" and
  "Home Ops", and issue identifiers such as `ACME-12`.
- By contributing you agree that your work is released under the
  [MIT License](LICENSE).

## Development setup

Home Assistant 2026.10 needs Python 3.14 and does not run on Windows; use
Linux, macOS, WSL or the dev container.

1. Open the repository in the dev container (VS Code: "Reopen in Container"),
   or create a Python 3.14 virtual environment yourself.
2. Install the requirements:

   ```bash
   scripts/setup
   ```

3. Start a development Home Assistant at <http://localhost:8123> with the
   integration loaded from this repository:

   ```bash
   scripts/develop
   ```

## Checks

```bash
scripts/lint   # ruff format + ruff check --fix
scripts/test   # pytest with coverage; fails under 95 %
```

CI also runs `hassfest` and the HACS validation action. The tests use
[pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component)
and a fake Paperclip server (`tests/conftest.py`), so they never contact a real
server.

## Paperclip API notes

Paperclip's REST API is unversioned and still in beta. The client in
`custom_components/paperclip/api.py` has no Home Assistant imports so it can
become a standalone library later. When you use a new route:

- treat a 404 from an optional route as "not supported" rather than an error;
- check the Paperclip source for the real response shape, because many schemas
  in the published OpenAPI document are loose;
- add the route to the fake server in `tests/conftest.py` with a neutral sample
  payload.

## Releasing (maintainers)

1. Make sure all workflows are green on `main`.
2. Set `version` in `custom_components/paperclip/manifest.json` (for example
   `0.2.0`) and, if needed, `homeassistant` in `hacs.json`. Commit and push.
3. Push the tag: `git tag v0.2.0 && git push origin v0.2.0`. Do not create the
   release by hand. The release workflow checks that the tag matches the
   manifest version and only then creates the GitHub release, with generated
   notes and `paperclip.zip` attached. A tag with a pre-release suffix (for
   example `v0.2.0b1`, matching a manifest version `0.2.0b1`) becomes a
   pre-release.
4. If the check fails, no release exists: delete the tag
   (`git push --delete origin v0.2.0`), fix the manifest and tag again.
5. Edit the generated notes on GitHub if users must do something before
   updating; put that at the top.
