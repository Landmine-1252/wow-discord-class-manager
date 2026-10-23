# Contributing

Keep the project focused on safe, simple main/alt management and class colors. Bug reports should include the image/version, relevant redacted log event names, reproduction steps and expected behavior. Never include real tokens, webhook URLs, `.env` files, real roster databases or full interaction payloads.

## Development

Use Linux (including WSL) with Python 3.13 and [uv](https://docs.astral.sh/uv/getting-started/installation/), or a Linux development container. The runtime image uses Python 3.13.17. The lock supports Python 3.13–3.14; CI uses 3.13. Direct Windows execution is not supported because storage locking and signals use POSIX APIs.

```sh
uv sync --locked
uv run --no-sync ruff check .
uv run --no-sync black --check .
uv run --no-sync pytest --cov=wow_class_manager --cov-report=term-missing
docker build -t wow-discord-class-manager:local .
```

No real Discord credentials are required for tests. To format changes, run `uv run black .` and `uv run ruff check . --fix`. Ruff's `I` rules enforce import ordering. In restricted environments use `black --workers 1` and a writable cache directory.

For real integration work create a separate test application and guild, use guild-scoped commands and a writable development database path as shown in [configuration](docs/configuration.md). Launch with `uv run --env-file .env python -m wow_class_manager`. Never point tests at a production token or database.

## Conventions

- Use typed, straightforward async Python; follow Black/Ruff formatting and import rules.
- Centralize class IDs/names/colors in `classes.py`. Preserve stable IDs and persistent component IDs across upgrades.
- Keep SQLite changes in transactional, versioned migrations; include an upgrade/persistence test.
- Keep character names in separate `primary_name` and `secondary_name` fields. Preserve display capitalization and stable character IDs. Full-name keys use Unicode normalization/casefolding within a member's guild roster.
- Treat a roster's main-character association as authoritative when character management is enabled. Switching mains must automatically demote the old main and preserve its record.
- Journal coordinated database/Discord operations before network writes; retain the previous committed roster until role confirmation. Exercise interruption, failed compensation and retry behavior in tests.
- Never infer names, race, faction or game ownership during migration. Preserve incomplete legacy names for explicit user completion.
- Preserve unrelated and disabled class roles. Never replace a member's entire role list or assign arbitrary user-provided role IDs.
- Test meaningful success/failure behavior, including race conditions and partial API failure when touching role changes.
- Keep admin authorization in the runtime path and suppress mentions/keep confirmations private.
- Add every new setting to `.env.example` and the configuration reference; explain operational changes in the appropriate guide.
- Dependency updates must regenerate `uv.lock` using `uv lock` (or `uv lock --upgrade-package NAME`) and pass checks. Never edit lock hashes manually. Pin updated container digests and review action release commits.

The [architecture guide](docs/architecture.md) describes boundaries and known transactional limits. See [releasing](docs/releasing.md) for publishing. Open a focused PR explaining the problem, resulting behavior and checks actually run. Keep credentials and generated runtime files out of commits.
