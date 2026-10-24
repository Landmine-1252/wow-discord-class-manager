# Security policy

## Report privately

Please report vulnerabilities through this repository's [GitHub private vulnerability reporting page](https://github.com/landmine-1252/wow-discord-class-manager/security/advisories/new). The maintainer must enable Private vulnerability reporting under Settings → Code security before release. Do not post exploit details, tokens, webhook URLs or private server data in public issues. If private reporting is not enabled, request a private reporting channel in an issue without disclosing vulnerability details. No response-time guarantee is made for this volunteer project.

Security fixes target the latest stable release. Use supported Python/container versions and review dependency update PRs. See the [release procedure](docs/releasing.md).

## Credentials and deployment

- Use a dedicated Discord application and bot token for each installation. Do not use personal user tokens or share an application with unrelated services.
- Treat the bot token and administrative webhook URL as passwords. Keep `.env`, mounted token files and backups private. Ignore rules and the Docker build allowlist exclude local secrets, but they do not protect a file once you deliberately add it to Git.
- Prefer a mounted token file. Environment values can be visible through host process/container inspection and diagnostic dumps. Do not post `docker inspect`, rendered Compose configuration or shell traces publicly.
- The bot needs Manage Roles and four panel channel permissions, **never Administrator**. Place it only as high in the role hierarchy as necessary.
- Class roles must remain cosmetic, with no role permissions or channel grants. The application refuses unsafe roles rather than clearing their permissions. Human administrators can change roles between API calls; do not use these roles for access control.
- Run one unprivileged container, keep the root filesystem read-only, and use a local persistent volume. Do not expose the Docker socket or add inbound ports. Restrict host administrator access; possession of the token enables the bot's Discord permissions.
- The optional webhook is outbound-only, host-validated and isolated from class changes. Avoid posting private context into configured panel text or webhook messages.

Logs redact configured credentials, recognizable tokens and webhook URLs, including exception formatting. Library payload debug logging is disabled even at application DEBUG level. This reduces accidental disclosure; it does not make publishing arbitrary diagnostic files safe.

## Rotate a compromised token

1. Stop the running bot container. If compromise is active, temporarily remove the app from the server or remove its role-management permission.
2. In the [Developer Portal](https://discord.com/developers/applications), choose your application → **Bot → Reset Token**. This invalidates the old token immediately.
3. Replace the token in exactly one configured source. Do not enter it directly into shell history or commit it.
4. Recreate the container (`docker compose up -d --force-recreate`, including your deployment overrides), then check logs and `/class status`.
5. Review Discord's audit log for unwanted roles or assignments and review the host for compromise. Preserve the existing database for the same application.
6. If a webhook URL was exposed, delete/recreate or rotate that webhook separately and update its configuration. Removing a secret from Git's latest commit is insufficient; rotate it and address repository history/caches as appropriate.

## Data retained

SQLite stores guild IDs, Discord member owner IDs, two-part self-reported character names, class IDs, gameplay roles, main-character associations, timestamps and minimal recovery journal data, alongside Discord role IDs, panel IDs and command-registration metadata. It contains no Battle.net credentials, game ownership verification, message contents or interaction payloads. Names and ownership are assertions by the Discord member, not verified Blizzard records.

Character edits and management replies are ephemeral and bound to the requesting member and guild. `/roster` displays registered characters only inside the same guild; `ROSTER_VISIBILITY=admins` restricts it to Manage Server holders, and `disabled` or `ENABLE_GUILD_ROSTER=false` disables guild-wide display. Members retain private access to their own roster. Server owners should explain their chosen roster visibility to members.

Members can remove their own characters through the confirmed removal workflow. Removing a main with alts requires a replacement; removing the last character follows `ALLOW_CLEAR_SELECTION`. A server officer with Manage Server and Manage Roles can use `/character purge` and explicitly confirm removal of a member's stored character data, including for someone who has left. Purge leaves existing Discord roles as a legacy class-only selection and does not depend on role API availability. This is for authorized data deletion; it is not an ownership transfer feature.

Leaving a server does not automatically delete characters. Guild and character state remains in the persistent volume so reinstall, rejoin and restore can retain it. Backups can retain deleted records until their retention period expires; protect them and document a suitable retention policy. Decommissioned deployments should have their volumes and backups removed deliberately. Do not publish real roster databases in issues or test fixtures.
