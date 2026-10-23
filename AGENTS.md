# Development conventions

Work in this repository without overwriting unrelated changes. Follow CONTRIBUTING.md and docs/architecture.md. Use Python 3.13+, discord.py 2.x, uv.lock, Black and Ruff; use `uv sync --locked` before checks.

- Run `uv run --no-sync ruff check .`, `uv run --no-sync black --check .`, and `uv run --no-sync pytest` after meaningful application changes. Build the container for dependency/deployment changes when Docker is available. Report checks honestly.
- Keep class IDs and component custom IDs stable. All class definitions belong in `classes.py`.
- Use targeted role PUT/DELETE operations with `atomic=True`; never replace member.roles. Recheck role safety. Preserve unrelated roles and disabled classes, and attempt recovery after partial failure.
- Apply guild allowlists and runtime admin checks on every relevant path. Never request Administrator or privileged intents.
- Do not log tokens, webhook URLs, full interactions or member payloads. Do not access or print an operator's local secrets for debugging.
- Use versioned, transactional SQLite migrations. No production data reset as an error-recovery shortcut. One process/volume only.
- Store primary and secondary character names separately. Scope every roster operation to its guild and owner; preserve IDs and automatically demote the old main by changing the single main pointer.
- Keep character operations on the shared member lock and role service. Persist recovery intent before Discord writes, confirm targeted role changes before committing the main, and retain recoverable journals after uncertain failures. Never hold SQLite write transactions across network calls.
- Document new environment settings in `.env.example` and docs/configuration.md. Keep first-run setup short and errors actionable.
- Maintain digest-pinned base images, SHA-pinned actions and a generated dependency lockfile. Do not publish, tag a release or contact a live guild unless authorized by the user.
- Keep tests independent of real Discord credentials and add regression cases for changed failure/race behavior. Do not claim live integration or ARM64 validation unless performed.
