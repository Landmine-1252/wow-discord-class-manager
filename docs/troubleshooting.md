# Troubleshooting

Start with `docker compose logs --tail=100 bot`, `docker compose ps`, and `/class status` in the affected server. Status requires Manage Server. Setup and sync require both Manage Server and Manage Roles. Substitute your configured command names when customized. Never paste `.env`, token files, webhook URLs or complete container inspect output into a public issue.

## Bot is online but commands do not appear

- Confirm Guild Install includes `bot` and `applications.commands`. Reopen the installation link if scopes were missing.
- Check `commands_ready` or `command_registration_failed` in logs. Failed registrations retry after 60 seconds.
- With `COMMAND_SYNC_SCOPE=guild`, commands appear only in `DEVELOPMENT_GUILD_ID`. Use `global` for normal deployment.
- Check Use Application Commands and Server Settings → Integrations restrictions. Reload the Discord client after allowing propagation time.
- Commands work only in server contexts. Keep Interactions Endpoint URL unset and privileged intents off.
- If an old root name persists, preserve the existing database when changing names/scopes; it records which scope needs cleanup.

## Bot cannot create roles

Give the bot Manage Roles and put its highest role above the class roles. Check `AUTO_CREATE_ROLES=true`. A role-name conflict is intentionally not “fixed” by creating another role. Discord may also refuse creation when the server has reached its role limit. Resolve the warning in `/class status` or `/class sync` and retry. Initial startup failures retry periodically without blocking other servers.

## Bot cannot assign roles

Run `/class sync` and `/class status`. Class roles must exist, be below the bot's top role, be unowned by integrations, have **zero role permissions**, and have no channel override granting any permissions. Check for permissions added after setup. Use dedicated cosmetic roles; do not remove meaningful security permissions from officer roles just to satisfy the bot.

The member must belong to the server and the server must be allowed. Disabled classes cannot be assigned through an old dropdown or forged command value. The bot never accepts a user-supplied role ID. After partial API failures it attempts to restore the prior managed class assignments and reports when that state cannot be confirmed. An officer should inspect the member before retrying if recovery failed.

## Roles appear duplicated

The bot never guesses between same-name roles. Rename the unwanted matching roles without deleting them, or configure a unique class role name. Run sync again. Stored role IDs are reused even after environment-driven name changes. A role already associated with another (including disabled) class is not reassigned.

Check whether multiple independent bots/apps or containers are active, whether the persistent volume was replaced, and whether a request was interrupted during role creation. Discord and SQLite do not share a transaction: after an abrupt crash the next sync normally recovers the uniquely named role. If multiple roles truly exist, an administrator must resolve the ambiguity. No automatic role deletion occurs.

## Class color does not appear / officer color wins

Discord uses the member's highest colored role. Inspect all the member's roles and their order. A higher officer/supporter role can override the class color even though class selection succeeded. Status reports higher colored unrelated roles. An administrator can change role order according to server policy, keeping the bot above class roles. The bot never moves roles or removes unrelated roles. White Priest is intentional; `#000000` as a custom color means Discord's default/no color.

## Character-management panel stops working after restart

Check that the **same Discord application**, SQLite volume and component version are in use. Verify the bot is online/healthy, the guild is allowed, and Interactions Endpoint URL remains empty. “This panel is no longer active” means the database points to another message or its state was lost; use the current panel, or restore the correct backup. Run setup to publish/recover a panel when necessary and manually remove orphan bot panels. Persistent public buttons survive restarts; an already-open ephemeral editing session can expire, so reopen Manage Characters. Back, paging and successful changes normally update the current private dashboard; they do not update the public panel. If you still see the old class dropdown, check `ENABLE_CHARACTER_MANAGEMENT`, recreate the container after environment changes, and allow startup to refresh the saved panel.

## Community Roster is missing, restricted or empty

The public Community Roster button and `/roster` require `ENABLE_CHARACTER_MANAGEMENT=true`, `ENABLE_GUILD_ROSTER=true` and `ROSTER_VISIBILITY` other than `disabled`. Recreate the container and let the existing panel refresh after changing settings. With `ROSTER_VISIBILITY=admins`, the button remains visible but only members with Manage Server can open it; everyone can still view their own characters privately.

Use Reset Filters to clear the class, main/alt and member selections. Results contain only characters registered in this server, including members' self-reported plans; the bot cannot discover characters from the game or another Discord server. A member with only a legacy class role has no character record until they provide both name parts.

## My character's Discord color is incorrect

Open My Characters and confirm which character is marked Main. Only its class determines the desired role; alts never add class roles. Opening character management checks the enabled main against current Discord roles. A higher unrelated colored role can still override the visible color; see the color-priority explanation above. `/class set` intentionally does not override a registered roster—use Change Main or edit the main's class.

If the main's class was disabled, its character record and designation stay intact. Select an enabled alt as main or edit the main to an enabled class when allowed. That explicit change removes obsolete safely managed class assignments, including the old disabled-class assignment. No character record or Discord role object is automatically deleted.

## An old main still appears as main

Reopen My Characters to fetch current data rather than relying on an old ephemeral reply. A successful promotion updates one main pointer and automatically makes the old main an alt. If a switch failed, retaining the old main is intentional: the bot reports success only after role confirmation and database commit. Retry from a fresh view after fixing the reported permission/connectivity problem. Pending operations are recovered against the prior committed roster; never manually alter main pointers or recovery rows.

## A member has multiple managed class roles

Open character management to reconcile an enabled main. The bot keeps that main's role and removes other safely associated class assignments through targeted operations, preserving unrelated roles. If a class role has acquired permissions, moved above the bot, or become integration-managed, the bot will refuse to touch it and explain the issue. Restore safe cosmetic role configuration or have an officer resolve it deliberately. Do not delete character records to repair roles.

## A character cannot be promoted

Confirm it belongs to your roster in this guild, its two name parts are complete, its class is enabled, and its configured class role exists and is manageable. `/class sync` can recreate an enabled role deleted by an administrator. A main switch needs Manage Roles on the bot and a class role below its highest role. Selecting a character from someone else's roster or another guild is refused, even if a component/autocomplete value is modified.

During a Discord outage, the prior valid main remains committed. A journal detects interrupted operations, and the next character interaction attempts recovery before a new mutation. If recovery is blocked by permission, hierarchy or storage problems, fix those first. Switching between two characters of the same class does not unnecessarily reassign the role.

## A two-part name is rejected

Use the separate Primary Name and Secondary Name fields. Both are required for new characters. Check the configured minimum/maximum for each part (default 2–12, application policy). Unicode letters and attached accent marks are accepted; spaces, leading/trailing whitespace, digits, punctuation, invisible formatting/control characters and Discord markup are rejected. Capitalization is preserved after safe Unicode normalization.

Duplicate comparison uses the full normalized name, case-insensitively, within your guild roster. John Stormwind and John Ironforge are different; John Stormwind and john stormwind are duplicates. Other Discord users may register the same full name. Passing bot validation does not prove the name is available, accepted or owned in the game.

## A character was renamed

Use Edit Character to change Primary Name and/or Secondary Name. The same internal character ID remains, preserving main/alt status and class. Future lists use the updated full name; an old private reply is a historical snapshot. Do not remove and re-register the character just to rename it. Changing an alt's class leaves the main role unchanged; changing the main's class synchronizes that role when class editing is enabled.

## A previously registered character is missing after migration

Confirm the same Compose project name, `/data` volume, database path, guild and Discord account are in use. Character ownership is per member **and** per guild. Original class-only versions stored no character names, so seeing an empty roster after upgrading one is expected; existing colored roles remain and registration starts the roster.

Supported custom single-name records preserve their original text and ID, with a missing secondary component marked for completion through Edit Character. They are not split or fabricated. Unknown/conflicting legacy schemas stop startup without silently dropping data. Preserve the original database and consult the [migration procedure](deployment.md#upgrading-an-existing-class-only-installation). Restoring an older image may require the matching pre-upgrade backup. Never replace a populated database with a blank one to bypass a migration error.

## Privacy deletion or a departed member's roster

Leaving the guild does not delete characters. An officer with Manage Server and Manage Roles can use `/character purge` for the target Discord user and explicitly confirm. This deletes that user's stored roster/recovery data in the current guild without depending on Discord role changes; existing roles remain a legacy selection. Other guilds and users are unaffected. Backups can retain deleted data until their retention period expires.

## Selection message or channel was deleted

Run `/class setup` in a suitable text channel. A deleted stored message is recreated; repeated setup reuses an existing bot-owned message. Startup also refreshes/recreates a stored message when the channel remains accessible. If the entire channel was deleted, explicitly choose a replacement with the `channel` option. Moving a panel disables only the old bot panel's controls; unrelated messages remain. If Discord denies editing the old panel, its callbacks are already inactive and the bot retries cleanup after restart.

## Environment changes do not appear

`docker compose restart` preserves the container's original environment. Run `docker compose up -d --force-recreate`, adding the build/secrets/restore overrides used by your deployment. Confirm you're editing the `.env` in the correct Compose directory. Single-quote literal `#`/`$` values. Invalid configuration exits with a setting-specific log message. Updating an existing panel requires channel access. Commands may need client propagation time.

## Container cannot write SQLite

Ensure `/data` is a writable volume owned by UID/GID `10001:10001`, with enough free disk space. SQLite must write WAL, SHM and lock files in the same directory. A read-only root filesystem is expected; writing the database outside the mounted directory is not. Do not use an NTFS/network bind mount for production SQLite. Check rootless Podman user mappings and SELinux labels. Another process holding the database lock is a configuration error; run only one instance. Corrupt or newer databases require restoration or a compatible image, not deletion. See [backup and restore](deployment.md#backup-and-restore).

## Bot disconnects repeatedly / container stays unhealthy

Check host clock, DNS and outbound HTTPS/WebSockets, then [Discord's service status](https://discordstatus.com/). Confirm host resources are adequate and no second process uses the same application. Gateway interruptions reconnect automatically. A stale heartbeat indicates an unresponsive loop/storage; `docker compose exec bot python -m wow_class_manager.health` gives the local probe result. Standard Compose does not restart a running container solely because it is unhealthy. Investigate logs, then restart if required. Do not open inbound ports or enable privileged intents as a workaround.

## GHCR image cannot be pulled

The image is unavailable until a successful publish workflow runs. Build locally with the supplied build override in the meantime. Confirm spelling, owner, tag and platform (AMD64 or ARM64). The owner must explicitly make the **package Public**; repository visibility alone is insufficient. `latest` is created only by stable release tags, while `edge` comes from main. Public pulls do not need authentication. Private packages require suitable credentials and are outside the public quick start. Docker Hub throttling during a local build concerns its base images, not the GHCR package.

## Token authentication fails

Copy the token from Developer Portal → Bot, not the application ID, public key, client secret or an OAuth token. Remove any `Bot ` prefix. Configure only one token source and make mounted files readable. Reset the token if it is invalid or exposed, update the secret, then recreate the container. Do not print it to check it. Login failures terminate with a clear error; they do not silently use another token. See [SECURITY.md](../SECURITY.md).

## Webhook alerts are missing

Alerts are optional and rate-limited per event/guild. Verify the URL belongs to an incoming Discord webhook at the supported HTTPS host/path and that it has not been deleted. `admin_notification_unavailable` or `admin_notification_rejected` reports failures without the secret URL. Class selection continues even when delivery fails.
