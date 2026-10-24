# Deployment and operations

## Install the container tools

On a Linux server, follow Docker's official [Engine installation instructions](https://docs.docker.com/engine/install/) for your distribution, then install the [Compose plugin](https://docs.docker.com/compose/install/linux/). Check `docker version` and `docker compose version`. Enable the Docker daemon at boot using your distribution's service manager (commonly `sudo systemctl enable --now docker`). Membership in the `docker` group is effectively root access; use your server's established access policy.

On Windows or macOS, [Docker Desktop](https://docs.docker.com/desktop/) includes Compose. Use Linux containers. On Windows, the WSL2 backend is recommended; keep a named volume for SQLite instead of placing the database on an NTFS bind mount. PowerShell can use `Copy-Item .env.example .env`; `chmod` examples below are for Linux/WSL. Make sure Docker Desktop is configured to start when you sign in; an always-on Linux host is preferable for unattended operation.

No ports are published. The bot opens outbound HTTPS and Gateway WebSocket connections to official Discord services. Allow DNS, TLS and WebSockets through your firewall; no reverse proxy, inbound firewall rule, public IP or port forwarding is needed.

## Configure and start

Clone the repository, copy `.env.example` to `.env`, and set one token source. Protect the file with `chmod 600 .env` on Linux. The default Compose configuration uses `ghcr.io/landmine-1252/wow-discord-class-manager:1.1.0`:

```sh
docker compose pull
docker compose up -d
docker compose logs --tail=100 -f bot
docker compose ps
```

The image must have been published first. Public packages can be pulled without signing in. The `1.1.0` default selects the character-management release; change `BOT_IMAGE` deliberately when upgrading. To make a deployment immutable, use the published `@sha256:...` manifest digest instead of a tag; copy the real digest from GHCR or `docker buildx imagetools inspect IMAGE`.

To build directly from this checkout:

```sh
docker compose -f compose.yaml -f compose.build.yaml build
docker compose -f compose.yaml -f compose.build.yaml up -d
```

Or build just the image with `docker build -t wow-discord-class-manager:local .`. Keep the build override in subsequent `up` commands so Compose continues using your local build. It does not need registry credentials. The base images and uv are digest-pinned; runtime dependencies come from `uv.lock`. The runtime contains no compiler, uv or development tools.

Both deployment methods create the named volume `wow-class-manager_bot-data`, mounted at `/data`. Changing the Compose project name changes the volume name. The root filesystem is read-only, `/tmp` is a small temporary filesystem, all Linux capabilities are dropped, and privilege escalation is disabled. Run exactly **one** container against this volume and token. Do not scale the service or share SQLite over a network filesystem.

## Start, stop and reconfigure

```sh
docker compose stop
docker compose start
docker compose restart
docker compose logs --tail=100 bot
```

`restart` restarts the current environment. After changing `.env`, use:

```sh
docker compose up -d --force-recreate
# For local builds:
docker compose -f compose.yaml -f compose.build.yaml up -d --build --force-recreate
```

On startup, stored roles follow their configured names/colors, panels refresh, and persistent components are registered before READY. Missing permissions produce diagnostics and background retries. Deleted messages are recreated in their stored channel when accessible, or by rerunning setup. API problems in one guild do not block other guilds.

`docker compose down` removes containers and networks but preserves the named volume. **`docker compose down -v` deletes that volume.** Do not use it for routine updates. On SIGTERM the process drains active interactions, cancels background tasks, disconnects, and closes SQLite. Compose allows 40 seconds for shutdown.

## Health and reconnection

Each startup checks SQLite integrity, foreign-key consistency and a rollback-only database write before connecting to Discord. It also checks that the local heartbeat file can be written. After authentication, logs identify the public bot/application IDs and command scope, then report installed/allowed servers, role safety and the saved panel's channel permissions and ownership. No token, webhook URL or member roster is printed.

Useful startup events:

| Event | Meaning / next step |
| --- | --- |
| `startup_checks_started` | Version and nonsecret configuration summary. |
| `startup_database_ok` / `startup_health_file_ok` | Local storage checks passed. A failure stops startup with an actionable error. |
| `discord_authenticated` | Compare the public application ID with the app you invited. |
| `commands_ready` | Command registration completed for the logged scope. |
| `startup_no_guilds` | Connected to Discord with no servers; install this application with Guild Install and the `bot` scope. |
| `startup_guild_missing` | An expected allowlisted or command-registration server is absent. Check its ID and installation. |
| `guild_skipped` | A joined server is outside the allowlist; no role changes are made there. |
| `guild_startup_check` | Per-server role and saved-panel diagnostics. |
| `guild_ready` | That server's role/panel preparation succeeded. |
| `guild_setup_needs_attention` / `guild_setup_failed` | Follow the diagnostic; preparation retries after 300 seconds. Other servers continue operating. |
| `startup_health` | Readiness changed to the reported state. |

A fresh installation has no saved panel, so the startup check tells an officer to run `/class setup`. This is not a health failure. Startup checks do not create test members, register fake characters, assign a test role or publish a first-run panel. They cannot prove that every member's visible color will match, because higher colored roles can override it; complete the [ordinary-member acceptance test](discord-setup.md#4-publish-and-test).

```sh
docker compose exec bot python -m wow_class_manager.health
```

The local probe reports these states:

| State | Meaning |
| --- | --- |
| `starting` | Initial checks or command registration are still pending. |
| `waiting_for_guild` | Discord is connected, but no installed server is eligible for management. Check the installation and allowlist. |
| `degraded` | A server check or command registration failed, or an expected server is missing. Check the log diagnostics; other ready servers remain usable. |
| `connected` | Gateway, command registration and eligible-server checks have passed. |
| `disconnected` | The Gateway is unavailable; automatic reconnection continues. |
| `unresponsive` | The heartbeat/process check failed or became stale. |

It reads an atomically written timestamp and PID, checks that the process exists, and rejects a heartbeat older than 45 seconds. The bot refreshes this file every 10 seconds after checking SQLite. Readiness requires a ready Gateway and a recent heartbeat acknowledgement as well as successful startup checks. Server diagnostics use cached startup/join results; the local probe does not make Discord API calls or scan members every ten seconds. Use `/class status` for a fresh server-specific report. There is no health-check network listener.

An explicitly configured application ID that differs from the authenticated application, or a configured Discord Interactions Endpoint URL, stops startup with instructions to correct it. The bot requires Gateway delivery; an HTTP interactions endpoint would route requests away from it.

Docker gives startup a 90-second grace period, then probes every 30 seconds. Only `connected` returns success; all other states return failure with an explanation. Joining an eligible server triggers checks automatically, including when the container started before the invite. A brief outage is handled by discord.py's reconnect logic. An unhealthy status alone **does not restart a container in standard Docker Compose**; the restart policy handles process exits. Inspect persistent unhealthiness and use `docker compose restart` if needed. Command-registration failures retry after 60 seconds.

## Docker secrets

Create the private file `./secrets/discord_bot_token` containing only the token and an optional trailing newline. Do not place a token in shell command arguments or shell history. Use a private editor or a password manager's file export.

```sh
mkdir -p secrets
chmod 700 secrets
# Create secrets/discord_bot_token with your editor, then:
chmod 644 secrets/discord_bot_token
```

The directory protects the file from other host users; the file mode permits UID 10001 to read the bind-mounted file inside the container. With regular Compose, file-backed secrets are bind mounts and `uid`/`gid` remapping is not portable. Keep the parent directory private. For a different host layout, grant only container UID 10001 read access using ownership or an ACL instead.

Set `DISCORD_BOT_TOKEN=` in `.env`. Then:

```sh
docker compose -f compose.yaml -f compose.secrets.yaml up -d
# Before a public release, combine the local build and secrets:
docker compose -f compose.yaml -f compose.build.yaml -f compose.secrets.yaml up -d --build
```

The override passes `DISCORD_BOT_TOKEN_FILE=/run/secrets/discord_bot_token` and an empty inline token. Docker's [Compose secrets guide](https://docs.docker.com/compose/how-tos/use-secrets/) explains the file mount model. A manually mounted read-only token file works too: configure its absolute container path and leave the inline token empty. Configuring two nonempty sources is rejected, even if they contain the same token.

## Updates and rollback

1. Read release notes and back up the database.
2. Change `BOT_IMAGE` to the selected version or digest.
3. Run `docker compose pull` and `docker compose up -d`.
4. Check the logs, health and `/class status`; test the existing panel and a private character workflow.

For local builds, fetch the selected source revision and use the build override. Do not overwrite local configuration. SQLite migrations run before Discord connects. A newer schema is rejected by older software: rollback may require restoring the pre-upgrade backup as well as the older image. Do not replace a newer database with an empty one to bypass a schema error.

## Upgrading an existing class-only installation

The character release upgrades the existing database to **schema version 3** automatically. Keep the same application/token, `DATABASE_PATH`, Compose project name and `/data` volume. No manual SQL, new container service or new network port is needed.

1. Stop the bot and take the consistent backup described below. Retain the old image version/digest alongside that backup.
2. Review the new settings in `.env.example`. Character management defaults to enabled, with unlimited characters and a member-visible guild roster. Choose `ROSTER_VISIBILITY=admins` or `disabled` if preferred before starting.
3. Keep your existing token and role overrides. Set `CHARACTER_PANEL_TITLE` for the new title. An old explicitly configured `PANEL_DESCRIPTION` is preserved; update it or remove the override to get the new character description.
4. Pull the chosen release and recreate the existing service, or use the normal local build override. Startup adds character, roster and recovery-journal tables without deleting guilds, role mappings or saved panel IDs.
5. Check health/logs and `/class status`, then open the **existing** public message. Its controls update in place to My Characters. Run `/class setup` only if it needs repair; no duplicate panel is needed.
6. Test John Stormwind as a main Paladin, Sam Ironforge as a Warrior alt, and switching between them. Confirm unrelated roles remain and both records survive a container restart.

Original class-only installations have no character names to migrate. Existing colored roles remain intact, and users register their real characters when ready. No fabricated character or automatic whole-server member scan occurs. The same Discord user can have independent rosters in different guilds.

For a recognized older custom character table with `id`, `guild_id`, `user_id`, `name` and `class_id`, migration preserves the original table as `legacy_characters_v2`, retains IDs and the original unsplit name, and leaves the secondary name empty for explicit completion through Edit Character. A single explicit existing main marker is retained; absent/ambiguous markers do not cause an arbitrary main choice. Unsupported schemas, conflicting names or invalid relationships stop migration transactionally with the original database intact. Do not delete records to bypass that error; preserve the backup and arrange a reviewed migration for the actual schema.

After an interrupted main switch, the next private character interaction reconciles the durable pending operation and the prior committed main. If permissions or connectivity still prevent recovery, the bot explains the problem and retains the roster and journal for retry. Do not clear recovery rows manually. Members who leave and rejoin retain their stored characters; their roles are reconciled when they use character management again.

To temporarily return to class-only behavior without rolling back the database, set `ENABLE_CHARACTER_MANAGEMENT=false` and recreate the container. Records remain intact, while legacy class selection becomes available. Re-enabling restores the stored main as the authoritative class when the member interacts. To run an older application image that only understands schema 2, restore the **matching pre-upgrade backup** as well; disabling a feature does not downgrade its schema.

## Backup and restore

The simplest consistent backup stops the bot, then copies the database using a temporary container from the same image. This includes any WAL journal files and preserves character records, main associations, pending recovery operations and role/panel associations. Backups contain self-reported character names and Discord owner IDs; keep them private. The fixed project name in the supplied Compose file gives the volume its default name below; adjust it if you changed the project name.

```sh
mkdir -p backups
chmod 700 backups
docker compose stop bot
docker run --rm --user 0:0 --entrypoint tar \
  -v wow-class-manager_bot-data:/data:ro \
  -v "$PWD/backups:/backup" \
  wow-discord-class-manager:local -C /data -czf /backup/bot-data.tar.gz .
docker compose start bot
chmod 600 backups/bot-data.tar.gz
```

Use your pinned GHCR image in place of `wow-discord-class-manager:local` when deployed from GHCR. The one-off backup helper runs as root solely to read UID 10001's private files; the bot remains unprivileged. Store backups separately from the host and protect `.env`/token files separately. Do not make a plain file copy of an actively writing WAL database. Advanced operators can use SQLite's online backup API instead.

To restore, first stop the bot and retain a backup of the current volume. Use a **new empty named volume** to avoid mixing WAL journals from different databases:

```sh
docker volume create wow-class-manager_restored-data
docker run --rm --user 0:0 --entrypoint sh \
  -v wow-class-manager_restored-data:/data \
  -v "$PWD/backups:/backup:ro" \
  wow-discord-class-manager:local \
  -c 'tar -xzf /backup/bot-data.tar.gz -C /data && chown -R 10001:10001 /data && chmod 700 /data'
```

Create a local `compose.override.yaml` to point the logical volume at the restored volume:

```yaml
volumes:
  bot-data:
    external: true
    name: wow-class-manager_restored-data
```

Then recreate the container. Default `docker compose up -d` automatically reads `compose.override.yaml`; when using explicit `-f` flags, include that override explicitly. Confirm the old panel works. Keep the previous volume until you have verified the restore.

If the database is missing, the application initializes it and safely discovers roles by name again. Customized names that no longer match and old panel IDs cannot be reconstructed reliably without a backup. Existing orphan panels are rejected; remove them manually before publishing a new one. Corrupt or inaccessible databases are **not** silently discarded.

## Volume permissions

The image runs as UID/GID `10001:10001`. A fresh Docker named volume inherits `/data` ownership from the image. For a host bind mount, prepare an empty directory owned by that UID/GID (`sudo chown 10001:10001 ./data; sudo chmod 700 ./data`) and mount it at `/data`. SQLite needs directory write access for its database, WAL, shared-memory and lock files. Do not make it world-writable or run the permanent bot as root. Container logs explain initialization failures without exposing credentials.

## Podman

The OCI image also supports Podman. Use `podman compose` with a compatible Compose provider, or `podman build -t wow-discord-class-manager:local .` and `podman run` with the same environment, named volume, read-only root filesystem and dropped capabilities. No Docker socket is required.

For rootless named-volume ownership, initialize a new dedicated volume explicitly:

```sh
podman volume create wow-class-data
podman run --rm --user 0:0 --entrypoint chown \
  -v wow-class-data:/data wow-discord-class-manager:local 10001:10001 /data
podman run -d --name wow-class-manager --env-file .env \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --cap-drop ALL --security-opt no-new-privileges \
  -v wow-class-data:/data --restart unless-stopped \
  wow-discord-class-manager:local
```

For direct `podman run --env-file`, use an env file with literal unquoted values; it does not necessarily use Compose's quote/interpolation rules. Prefer Compose with the supplied `.env.example`. On SELinux hosts, private bind mounts may need `:Z`. Rootless bind mounts require correct user-namespace ownership (`podman unshare chown`) or an appropriate idmapped mount. Use a Podman Quadlet/systemd unit and user lingering for reliable start at boot; Docker's daemon restart semantics do not automatically apply to rootless Podman. Podman's support for `init`, logging drivers and health actions depends on its Compose provider. Validate `podman compose config` on the target host.

## Token rotation and connectivity failures

Stop the bot, reset the token in **Developer Portal → Bot**, replace the one configured secret source, then recreate the container. Verify logs and the existing panel. The same Discord application retains its role/panel associations; a new application has a different bot identity and cannot edit the old bot's panel.

For repeated disconnects, check system time, DNS, outbound TLS/WebSocket access, host memory and [Discord status](https://discordstatus.com/). Do not enable privileged intents or open inbound ports. Authentication errors require a valid bot token; permission errors require role/channel configuration. See [troubleshooting](troubleshooting.md).
