# Configuration reference

Compose reads `.env` and passes its values to the container. The Python application reads the process environment only; it does not load dotenv files itself. For local development use `uv run --env-file .env python -m wow_class_manager` with writable paths.

Precedence: **built-in defaults → environment variables → individual role overrides over the role-name template**. SQLite stores resource IDs, character rosters and recovery/command metadata, not competing operator settings. Stored role IDs take precedence over name discovery; the environment remains authoritative for each enabled role's desired name/color and panel text. Restart/recreate after changing the environment. Blank optional IDs, allowlists, webhook and footer mean “unset”; blank booleans, classes, names or colors are invalid.

Only one token source is required. Other variables are optional unless their documented feature is selected. Invalid configuration is rejected before connecting, with the setting name but no supplied secret value. The default inline placeholder in `.env.example` is deliberately invalid.

## Credentials, scope and roles

| Variable | Purpose | Default | Required? | Example / security notes |
| --- | --- | --- | --- | --- |
| `DISCORD_BOT_TOKEN` | Bot authentication | unset | One token source | Copy token from Bot page; secret; no `Bot ` prefix. Never commit it. |
| `DISCORD_BOT_TOKEN_FILE` | Read token from UTF-8 mounted file | unset | Alternative token source | `/run/secrets/discord_bot_token`; readable by UID 10001. Both nonempty sources are rejected. |
| `DISCORD_APPLICATION_ID` | Explicit application ID | auto-discovered | No | `123456789012345678`; public ID, must belong to the token's app. |
| `ALLOWED_GUILD_IDS` | Servers allowed to use the bot | empty: all installed guilds | No | `123456789012345678,234567890123456789`; all interactions and mutations are rejected outside this list. Bot stays installed but inactive there. |
| `DEVELOPMENT_GUILD_ID` | Development command target | unset | When scope is `guild` | `123456789012345678`; must be allowed if using an allowlist. |
| `COMMAND_SYNC_SCOPE` | Registration target | `global` | No | `global` or `guild`; the latter requires a development guild ID. Does not itself restrict role reconciliation; use the allowlist for that. |
| `ENABLED_CLASSES` | Class IDs to show/manage | `warrior,paladin,hunter,rogue,priest,shaman,mage,warlock,druid` | No | `paladin,mage,death_knight`; case-insensitive IDs, no duplicates or empty entries. Displayed choices are alphabetical by class name. |
| `AUTO_CREATE_ROLES` | Create missing roles | `true` | No | `false` means discover/update existing safe roles only. Missing roles remain unavailable. |
| `ROLE_NAME_TEMPLATE` | Default visible role names | `{name}` | No | `WoW {name}`; only `{name}` is accepted. Names must be 1–100 characters and unique across all 13 classes, case-insensitively. |

Changing a role-name environment variable renames the role already associated with that class ID. It does not create a new role. A manual Discord rename is brought back to the configured name on reconciliation. Role updates change only the name and primary color, preserving preexisting display/mention settings and permissions. All permissions must remain disabled for a role to be managed.

Reconciliation runs at initial ready/startup, guild join, `/class sync`, and `/class setup`. Initial failures retry every five minutes; an officer can run sync immediately after fixing permissions. Registered members' roles are also checked against their main when they interact; this does not scan every guild member. Disabled classes retain their character records, associations, role names/colors and member assignments. New characters or promotions cannot choose a disabled class. A main whose class becomes disabled stays main until the member explicitly edits or replaces it; the bot explains the problem instead of choosing an alt.

Disabling a class by itself does not mutate member roles. When a member explicitly switches to an enabled registered main, removes their last character, or reconciles an enabled main through character management, all other safely associated class roles are obsolete, including a previously disabled-class assignment. The role objects and character records remain intact. The legacy class-only selector keeps its original enabled-only cleanup policy. No automatic role deletion or bulk member cleanup occurs.

## Characters and guild roster

All class dropdowns and slash-command class choices show only `ENABLED_CLASSES`, alphabetically. Stored characters from disabled classes remain visible in personal rosters and the guild roster's **All classes** view. When editing one, leave the optional Class field empty to preserve its saved class while renaming it, or select an enabled class explicitly.

| Variable | Purpose | Default | Required? | Example / notes |
| --- | --- | --- | --- | --- |
| `ENABLE_CHARACTER_MANAGEMENT` | Enable main/alt workflows and character panel | `true` | No | `false` restores legacy class-only behavior while retaining character records. Re-enabling restores main-authoritative role reconciliation. |
| `CHARACTER_COMMAND_ROOT` | Character slash-command group | `character` | No | `hero`; 1–32 lowercase ASCII letters, digits, `_` or `-`. Must differ from the active class group and active `roster` command. |
| `MAX_CHARACTERS_PER_USER` | Per-user, per-guild character cap | `0` (unlimited) | No | `100`; nonnegative integer up to 2147483647. Lowering a limit never deletes records; it blocks further additions until below the limit. Lists are paginated independently of this setting. |
| `CHARACTER_PRIMARY_NAME_MIN_LENGTH` | Minimum first-name length | `2` | No | `1`; 1–50, no greater than its maximum. |
| `CHARACTER_PRIMARY_NAME_MAX_LENGTH` | Maximum first-name length | `12` | No | `20`; 1–50, no smaller than its minimum. |
| `CHARACTER_SECONDARY_NAME_MIN_LENGTH` | Minimum secondary-name length | `2` | No | `2`; 1–50, no greater than its maximum. |
| `CHARACTER_SECONDARY_NAME_MAX_LENGTH` | Maximum secondary-name length | `12` | No | `20`; 1–50, no smaller than its minimum. |
| `ENABLE_GUILD_ROSTER` | Enable Community Roster and guild-wide `/roster` | `true` | No | `false` hides the public roster button and command; private personal rosters remain available. |
| `ROSTER_VISIBILITY` | Guild roster access | `members` | No | `members`, `admins` (Manage Server), or `disabled`. The button remains visible for `admins`, but every access checks permissions. `disabled` hides it and the command. Guild roster is unavailable when character management itself is disabled. |
| `CHARACTER_PANEL_TITLE` | Character-manager embed title | `World of Warcraft Character Manager` | No | `Guild Characters`; 1–256 characters. `PANEL_TITLE` remains the legacy class panel title. |
| `ALLOW_CHARACTER_CLASS_EDIT` | Permit changing a stored character's class | `true` | No | `false` still allows name edits, adding a correctly classified character, and switching mains. |

The default public panel has **Manage Characters**, **My Characters** and **Community Roster** buttons. Private controls provide Add Character, an inline Change Main dropdown, Edit Character and Remove Character. The first character becomes main; all later characters become alts. Promoting an alt automatically demotes the previous main while preserving both records. The dashboard refreshes after successful changes; paging, Back and Cancel navigate within the private message. `/character add`, `list`, `main`, `edit`, `remove` and `help` provide the same workflows. `/character purge` requires Manage Server and Manage Roles plus confirmation to delete a user's stored roster, including a departed user's records; existing Discord roles remain as a legacy selection.

Community Roster opens a private browser of this server's self-reported characters, with class, main/alt and member dropdown filters, pagination and Reset Filters. These controls follow the same visibility setting as `/roster`. Planned characters are welcome: members provide both name parts and can edit their plans later. Registration does not reserve a game name or verify that a character already exists.

With a registered roster and character management enabled, `/class set` and `/class clear` cannot silently override its main; they direct the member to character management. `ALLOW_CLEAR_SELECTION=true` permits removing the only registered character. If a main has alts, removal requires selecting a replacement first regardless of that setting.

### Two-part name policy

The visible **First Name** field retains the internal `primary_name` identifier and the existing `CHARACTER_PRIMARY_NAME_MIN_LENGTH` / `CHARACTER_PRIMARY_NAME_MAX_LENGTH` settings. No configuration rename or database migration is needed.

Enter **First Name** and **Secondary Name** in separate required fields. `John Stormwind`, `John Ironforge` and `Sam Ironforge` can coexist in one roster. `John Stormwind` and `john stormwind` cannot. Display capitalization is preserved; Unicode NFC normalization makes equivalent composed/accented forms compare reliably, and the database key additionally uses Unicode casefolding. Legitimate Unicode letters and attached combining marks are accepted. Spaces inside a component, surrounding whitespace, punctuation, digits, invisible/control characters, malformed Unicode and Discord mention/formatting syntax are rejected rather than silently rewritten.

The configured lengths apply after normalization and are **application defaults**, not confirmed separate Blizzard name-length limits. Blizzard describes two-part names whose complete combination is unique within a region; this bot only checks duplicates within the same Discord member's guild roster. Other members can register the same full name because ownership is self-reported. It does not contact Blizzard to validate availability, ownership, naming-policy approval, reserved words or restricted spelling patterns. [Official naming overview](https://news.blizzard.com/en-us/article/24304161/create-a-name-of-your-own-in-wow-forever)

Changing name limits does not rewrite or delete existing characters. Supported imported single-name records retain the original string and are marked for completion; use Edit Character to supply the real first and secondary parts. The original legacy name remains preserved. Race and faction are not inferred or required in this release.

## Panel and behavior

| Variable | Purpose | Default | Required? | Example / notes |
| --- | --- | --- | --- | --- |
| `PANEL_TITLE` | Legacy class-only embed title | `Choose Your Class` | No | `Which class do you play?`; 1–256 characters; use `CHARACTER_PANEL_TITLE` for character mode. |
| `PANEL_DESCRIPTION` | Active panel description | Character mode: `Share the characters you plan to play, choose your main, and get to know the community. Your main sets your Discord class color. You can change your plans anytime.` | No | 1–4096 characters. When unset and character management is disabled, defaults to the original class-selection description. An explicitly configured value always wins. |
| `PANEL_PLACEHOLDER` | Legacy class dropdown prompt | `Select your class...` | No | `Choose your class...`; 1–150 characters. Character management uses buttons and private selection prompts. |
| `PANEL_FOOTER` | Optional embed footer | empty | No | `You can change this later.`; 0–2048 characters. |
| `PANEL_ACCENT_COLOR` | Embed accent color | `#5865F2` | No | `#3FC7EB`; exactly six hex digits, optional leading `#`. |
| `ALLOW_CLEAR_SELECTION` | Permit removing the only character or clearing a legacy selection | `false` | No | `true`; registered users use character removal rather than `/class clear`. Character removal clears safely managed class assignments; legacy clear only handles enabled classes. |
| `ROLE_CHANGE_COOLDOWN_SECONDS` | Delay between one member's attempts | `3` | No | `0` disables; finite number from 0 to 3600. Also applies to failed attempts and clearing. |
| `LOG_LEVEL` | Application log verbosity | `INFO` | No | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`; library payload debug logging stays disabled. |
| `ADMIN_WEBHOOK_URL` | Best-effort administrative alerts | unset/disabled | No | `https://discord.com/api/webhooks/ID/TOKEN`; secret. Only the exact HTTPS `discord.com` host, standard webhook path (optionally `/api/v10/`), numeric ID and token are accepted; no redirects, query, fragment, alternate port or user info. |
| `DATABASE_PATH` | SQLite file | `/data/classes.sqlite3` | No | `/data/guild.sqlite3`; absolute writable path. File, WAL, SHM and process lock stay together. |
| `HEALTH_PATH` | Local process heartbeat | `/tmp/wow-class-manager-health.json` | No | Absolute writable file path, distinct from the database; the container probe uses the same setting. |
| `BOT_IMAGE` | Compose image selection | `ghcr.io/landmine-1252/wow-discord-class-manager:1.1.0` | No | Compose-only; the release must be published before pulling. Use a real published digest for immutable deployment. The build override selects `wow-discord-class-manager:local`. |

The active title, description and footer together must fit within 6000 characters. Discord markdown is preserved in configured panel text; mentions are suppressed in all messages and responses. Character names are safely escaped for display. NUL characters are rejected. Single-quote values containing `#`, `$`, spaces or markdown in Compose `.env` files to prevent interpolation surprises. Do not put secrets in panel text. Canonical class names remain selection labels regardless of cosmetic role-name overrides.

Optional alerts cover setup and administrative failures. They are bounded, deduplicated for five minutes per event/guild, use a ten-second HTTP timeout and never block ordinary class changes. No ordinary class-change alerts or member payloads are sent. The URL is never included in status or logs.

## Slash-command names

| Variable | Purpose | Default | Required? | Example |
| --- | --- | --- | --- | --- |
| `SLASH_COMMAND_ROOT` | Existing class command group | `class` | No | `wow`; distinct from enabled character/roster roots. |
| `SLASH_SETUP_NAME` | Panel setup subcommand | `setup` | No | `panel` |
| `SLASH_SYNC_NAME` | Role synchronization subcommand | `sync` | No | `roles` |
| `SLASH_STATUS_NAME` | Diagnostics subcommand | `status` | No | `diagnose` |
| `SLASH_SET_NAME` | Member selection subcommand | `set` | No | `choose` |
| `SLASH_ME_NAME` | Member's current class | `me` | No | `mine` |
| `SLASH_CLEAR_NAME` | Clear selection subcommand | `clear` | No | `reset` |
| `SLASH_HELP_NAME` | Help subcommand | `help` | No | `guide` |

Names use a conservative subset of Discord's allowed syntax: 1–32 lowercase ASCII letters, digits, `_` or `-`. Class subcommand names must be unique. The character group's root is separately configurable using `CHARACTER_COMMAND_ROOT`; its subcommand names and the guild `/roster` name are fixed. Renaming commands updates the application-owned command tree on restart and removes old names. Changing scope cleans up the previously persisted scope after registering the new one. Registration is not repeated on Gateway reconnects. The bot fetches registered command names on startup and writes only when the configuration or known scope changed, or expected commands are absent. Do not share this Discord application with another bot that owns slash commands.

Global changes can take time to appear in Discord clients. Guild commands are intended for rapid development. If you switch scopes after losing the database, the bot no longer knows the old guild registration; use the original database or remove stale application commands manually. Keep the database during normal updates.

## Individual class overrides

Each optional `ROLE_NAME_*` overrides the template for that class. Each optional `ROLE_COLOR_*` overrides its official default. The following table lists **every supported variable**; defaults shown for names assume the default template. Use underscores in multiword IDs and environment suffixes. Class IDs never change when a role is renamed.

| Variable | Purpose | Default | Required? | Example / notes |
| --- | --- | --- | --- | --- |
| `ROLE_NAME_WARRIOR` | Warrior role name (`warrior`) | `Warrior` | No | `Guild Warrior`; overrides template. |
| `ROLE_COLOR_WARRIOR` | Warrior color | `#C69B6D` | No | `#112233`; six hex digits. |
| `ROLE_NAME_PALADIN` | Paladin role name (`paladin`) | `Paladin` | No | `Guild Paladin`; overrides template. |
| `ROLE_COLOR_PALADIN` | Paladin color | `#F48CBA` | No | `#112233`; six hex digits. |
| `ROLE_NAME_HUNTER` | Hunter role name (`hunter`) | `Hunter` | No | `Guild Hunter`; overrides template. |
| `ROLE_COLOR_HUNTER` | Hunter color | `#AAD372` | No | `#112233`; six hex digits. |
| `ROLE_NAME_ROGUE` | Rogue role name (`rogue`) | `Rogue` | No | `Guild Rogue`; overrides template. |
| `ROLE_COLOR_ROGUE` | Rogue color | `#FFF468` | No | `#112233`; six hex digits. |
| `ROLE_NAME_PRIEST` | Priest role name (`priest`) | `Priest` | No | `Guild Priest`; overrides template. |
| `ROLE_COLOR_PRIEST` | Priest color | `#FFFFFF` | No | `#112233`; six hex digits. |
| `ROLE_NAME_SHAMAN` | Shaman role name (`shaman`) | `Shaman` | No | `Guild Shaman`; overrides template. |
| `ROLE_COLOR_SHAMAN` | Shaman color | `#0070DD` | No | `#112233`; six hex digits. |
| `ROLE_NAME_MAGE` | Mage role name (`mage`) | `Mage` | No | `Guild Mage`; overrides template. |
| `ROLE_COLOR_MAGE` | Mage color | `#3FC7EB` | No | `#112233`; six hex digits. |
| `ROLE_NAME_WARLOCK` | Warlock role name (`warlock`) | `Warlock` | No | `Guild Warlock`; overrides template. |
| `ROLE_COLOR_WARLOCK` | Warlock color | `#8788EE` | No | `#112233`; six hex digits. |
| `ROLE_NAME_DRUID` | Druid role name (`druid`) | `Druid` | No | `Guild Druid`; overrides template. |
| `ROLE_COLOR_DRUID` | Druid color | `#FF7C0A` | No | `#112233`; six hex digits. |
| `ROLE_NAME_DEATH_KNIGHT` | Death Knight role name (`death_knight`) | `Death Knight` | No | `Guild Death Knight`; overrides template. |
| `ROLE_COLOR_DEATH_KNIGHT` | Death Knight color | `#C41E3A` | No | `#112233`; six hex digits. |
| `ROLE_NAME_MONK` | Monk role name (`monk`) | `Monk` | No | `Guild Monk`; overrides template. |
| `ROLE_COLOR_MONK` | Monk color | `#00FF98` | No | `#112233`; six hex digits. |
| `ROLE_NAME_DEMON_HUNTER` | Demon Hunter role name (`demon_hunter`) | `Demon Hunter` | No | `Guild Demon Hunter`; overrides template. |
| `ROLE_COLOR_DEMON_HUNTER` | Demon Hunter color | `#A330C9` | No | `#112233`; six hex digits. |
| `ROLE_NAME_EVOKER` | Evoker role name (`evoker`) | `Evoker` | No | `Guild Evoker`; overrides template. |
| `ROLE_COLOR_EVOKER` | Evoker color | `#33937F` | No | `#112233`; six hex digits. |

## Complete examples

Replace credential placeholders with your own secret. These are entire minimal environment files for their respective scenarios; omitted settings use the defaults above.

### Minimal installation

```dotenv
DISCORD_BOT_TOKEN=replace-with-your-discord-bot-token
```

### Customized guild

```dotenv
DISCORD_BOT_TOKEN=replace-with-your-discord-bot-token
ALLOWED_GUILD_IDS=123456789012345678
ENABLED_CLASSES=warrior,paladin,hunter,rogue,priest,shaman,mage,warlock,druid,death_knight
ROLE_NAME_TEMPLATE='WoW {name}'
ROLE_NAME_PALADIN='Guild Paladin'
ROLE_COLOR_PALADIN='#F48CBA'
CHARACTER_PANEL_TITLE='Register your guild characters'
PANEL_DESCRIPTION='Add your main and alts here. Changing mains automatically updates your class role.'
PANEL_FOOTER='One main character per member'
SLASH_COMMAND_ROOT=wow
CHARACTER_COMMAND_ROOT=hero
MAX_CHARACTERS_PER_USER=0
ROSTER_VISIBILITY=admins
ALLOW_CLEAR_SELECTION=true
ROLE_CHANGE_COOLDOWN_SECONDS=5
```

### Development/testing

```dotenv
DISCORD_BOT_TOKEN=replace-with-a-separate-test-bot-token
ALLOWED_GUILD_IDS=123456789012345678
DEVELOPMENT_GUILD_ID=123456789012345678
COMMAND_SYNC_SCOPE=guild
ENABLED_CLASSES=paladin,mage
LOG_LEVEL=DEBUG
DATABASE_PATH=/tmp/wow-dev.sqlite3
HEALTH_PATH=/tmp/wow-dev-health.json
```

Use a separate app, server and database from production. For production containers restore the database path to `/data`; `/tmp` is ephemeral in the supplied Compose deployment.

### Docker secrets

```dotenv
DISCORD_BOT_TOKEN_FILE=/run/secrets/discord_bot_token
ALLOWED_GUILD_IDS=123456789012345678
```

Create the token file and use `compose.secrets.yaml` as described in [deployment](deployment.md#docker-secrets). Leave `DISCORD_BOT_TOKEN` absent or empty. Mounted tokens are stripped of surrounding whitespace. An unreadable file, invalid token format or two configured sources stops startup without connecting.
