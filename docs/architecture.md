# Architecture

The bot is one Python process with one discord.py client, one SQLite database and no inbound services. It authenticates with its own operator's Discord bot token, connects to the Gateway, and receives slash-command and component interactions there. The Gateway makes a public interactions endpoint, public key, browser and reverse proxy unnecessary. Only the nonprivileged Guilds intent is enabled. discord.py owns reconnect and API rate-limit behavior.

## Module boundaries

| Module | Responsibility |
| --- | --- |
| `classes.py` | Immutable stable IDs, canonical labels and default colors for all classes |
| `config.py` | Typed environment configuration, validation and secret-free errors |
| `storage.py` | Serialized SQLite access, migrations, panel state and process lock |
| `character_names.py` | Separate name-part validation, NFC display normalization and Unicode comparison keys |
| `character_store.py` | Character records, owner-scoped rosters, relational constraints and durable operation journal |
| `characters.py` | Main/alt policy and coordination between the existing role service and character transactions |
| `roles.py` | Guild locks, safety rules, name discovery and reconciliation |
| `members.py` | Per-member locking, fresh member reads, targeted role changes and recovery |
| `ui.py` | Persistent select/buttons, embeds, safe panel reuse/movement |
| `commands.py` | Runtime authorization, interaction dispatch and private responses |
| `character_ui.py` | Persistent entry buttons, private owner-bound paginated workflows and name modals |
| `character_commands.py` | Character slash commands, character autocomplete and guild roster command |
| `roster_ui.py` | Private community roster browsing with class, main/alt and member dropdown filters |
| `diagnostics.py` | Read-only guild, role hierarchy and panel diagnostics |
| `bot.py` | Client lifecycle, command synchronization, startup recovery, background tasks |
| `notifications.py` | Bounded, nonblocking administrative notification queue |
| `logging_setup.py` | JSON formatting and final secret redaction |
| `health.py` | Atomic local heartbeat and independent health probe |
| `__main__.py` | Configuration, signal handling and exit codes |

## Role reconciliation

For each enabled class, reconciliation first uses its stored role ID. If the role disappeared or no mapping exists, it searches names case-insensitively. Exactly one safe match can be adopted. A missing role is created only if configured; conflicts are reported and never resolved by guessing. New roles are cosmetic, non-mentionable and not hoisted. Adopted roles keep their permissions, mention and hoist settings; permission-bearing roles are rejected rather than stripped.

Safety is deliberately conservative: no role permissions, no channel permission grants, no `@everyone`, no integration ownership, and a manageable role position below the bot. Checks apply to both adopted and already-associated roles, and run again before assignment. Guild/channel state comes from the Guilds Gateway events, while role lists are refreshed from the REST API. Changes by administrators outside this process cannot be made transactional with the bot's HTTP requests; administrators should not repurpose class roles as access-control roles.

Reconciliation holds a per-guild lock so concurrent startup/setup/sync cannot create duplicates. Each association is persisted immediately. Failures in an individual class/guild do not abort unrelated guilds. No operation deletes roles. Disabling classes alone stops their management without changing members or deleting characters. An explicit character-driven switch to an enabled main, or reconciliation of that main, removes other safely managed class assignments even when an obsolete class was disabled; the class-only legacy path continues to remove enabled assignments only.

## Character and roster model

A Discord **guild** contains member-specific rosters keyed by `(guild_id, user_id)`. A **Discord member** owns zero or more registered **characters** in that guild. A character stores `primary_name`, `secondary_name` and a stable `class_id`; `full_name` is a derived display value, not a substitute for the separate fields. Each roster points to one `main_character_id`. Every other owned character is an alt by definition; there is no separate alt flag to maintain.

The main's class ID resolves through the existing centralized class registry and guild role-ID mapping. Alts never assign roles and there are no per-character roles or separate main/alt role sets. Changing the main changes a single association, so the prior main automatically becomes an alt without modifying or duplicating its character record. Character IDs remain stable through name or class edits. The same Discord user in two guilds has entirely separate rosters.

The first newly registered character becomes main. A populated newly created roster always has one main, enforced by the storage mutation API and a single main pointer; a composite foreign key ensures that pointer belongs to the same member and guild. A recognized legacy roster can temporarily lack a main when its source had no unambiguous explicit designation. It requires user completion/selection, not a guessed promotion.

Names use NFC normalization for display and NFC/casefolded full-name keys for duplicate detection. A unique `(guild_id, user_id, name_key)` constraint permits shared primary names, shared secondary names and the same full name across different owners/guilds. New names require two components; Unicode letters and attached combining marks are accepted, while whitespace, formatting controls and punctuation are rejected. The configured limits are application policy, not a claim about Blizzard's separate component limits or a check of game availability/ownership. Imported incomplete names retain their original text for explicit completion. Optional race/faction metadata can be added through later additive columns; neither is inferred or collected now.

## Member transactions

The bot fetches the requesting member individually on each role operation, avoiding stale interaction role lists without subscribing to privileged member events. Discord is authoritative for actual assigned roles. When character management is enabled and a roster exists, its main is authoritative for the **desired** class role; legacy `/class set` and `/class clear` cannot override it silently. Class-only members keep their original behavior until registering a character.

A per-member lock serializes requests by that member. The operation waits for any ongoing guild reconciliation to finish reading mappings, then releases the guild lock; different members can proceed independently. It validates the enabled target and role safety, adds the target using the single-role PUT API (`atomic=True`), removes obsolete managed class assignments using individual DELETE calls, then fetches the member again to confirm the result. Legacy changes limit cleanup to enabled classes; character-driven changes consider all safely associated class roles. It never calls member role-list replacement and never edits the public panel on selection.

If any call fails or the result cannot be confirmed, recovery reads fresh state and attempts to restore the previously held managed class roles, checking safety again. It removes only a newly requested class role it introduced and preserves unrelated roles. Success is never reported for a failed/unconfirmed operation. Discord does not offer a multi-role transaction: temporary overlap, network ambiguity and failed rollback remain possible and produce explicit private diagnostics. External moderators and other bots are not coordinated by the local locks.

Graceful shutdown stops accepting work, waits up to 15 seconds for active interactions, then cancels remaining operations with a bounded recovery attempt. Hard kills or host power loss may leave a pending character operation; recovery handles it on the member's next interaction. Cooldowns remain short-lived in-memory deadlines.

## Coordinating mains and Discord roles

SQLite and Discord do not share a transaction. Character mutations use a per-guild-member lock shared with legacy role changes, and a durable **prepare → role confirmation → commit** sequence:

1. Load the owner's roster and recover any pending operation before allowing another mutation. Validate ownership, name uniqueness, class availability and the requested action. A main's removal with alts requires an explicit replacement; the old record remains until that replacement succeeds.
2. Resolve the existing role manager's safe desired-role plan and persist a journal entry containing an operation ID, action payload and previous/wanted managed role IDs with their stable class IDs. Recovery can resolve a recreated role through its current association, including a legacy class selection interrupted during first registration. The old roster remains committed. SQLite does not hold a write transaction while waiting for Discord.
3. Apply only targeted role operations through the existing member-role service and fetch the member to confirm the result. Same-class main changes perform no unnecessary role remove/add. Alt additions/edits do not change the main's role.
4. In a short SQLite transaction, apply the character mutation/main pointer and delete the matching journal entry. A success response follows only after both the required Discord result and database commit succeed.

If Discord fails, the old committed roster remains authoritative and the existing role service attempts safe compensation. If the database commit fails after Discord changed roles, compensate toward the prior committed state. Keep the journal when completion or recovery cannot be confirmed; report the failure and make a retry safe. A crash before commit leaves the prior roster plus the pending intent; the next interaction detects it and reconciles against that prior main, or the saved legacy-role state when no registered main existed. A crash after the transaction commits leaves the new main and no journal. No whole-guild member scan or privileged intent is needed.

Role hierarchy failures, missing associations and disabled classes produce actionable errors instead of arbitrary replacement mains. Members keep disabled-class character records. A disabled current main is not automatically changed; the user can select an enabled character or edit the main's class when editing is permitted. For an enabled main, interaction-time reconciliation repairs manually removed or duplicate managed class assignments. Departed members' stored rosters remain and can reconcile when they rejoin and interact.

Administrator-confirmed `/character purge` is a separate privacy operation: it atomically deletes the target member's stored character/roster/recovery data in that guild and leaves Discord roles as a legacy class-only selection. It requires Manage Server and Manage Roles and can target a user who has left. It does not depend on a successful Discord role API call. Ordinary member removal still follows the replacement-main/last-character policy and synchronizes roles.

## Persistence and panel identity

SQLite tables:

- `guilds`: guild IDs.
- `roles`: `(guild_id, class_id) → role_id`, unique per guild/role.
- `panels`: the active channel and message ID for each guild.
- `retired_panels`: old bot panels awaiting removal of their controls.
- `metadata`: application-specific command scope/fingerprint.
- `characters`: stable ID, guild/owner IDs, separate name parts, normalized full-name key, class ID, timestamps and preserved legacy name when applicable.
- `rosters`: `(guild_id, user_id) → main_character_id`, with a composite foreign key enforcing ownership.
- `character_operations`: one pending mutation per guild/member, identified by a unique operation ID and carrying the minimal recovery plan.

`PRAGMA user_version` tracks migrations. Version 1 establishes guild/role/panel state; version 2 adds command metadata and retired-panel cleanup; version 3 adds character rosters and recovery operations. Migrations run transactionally and do not rebuild or delete the existing guild/role/panel tables. Original class-only databases add empty character tables; no names are inferred and no Discord role changes occur during migration.

A recognized custom legacy table with `id`, `guild_id`, `user_id`, `name` and `class_id` is retained as `legacy_characters_v2` and copied without splitting its name or changing IDs. The original string is preserved in `primary_name` and `legacy_name`, with an empty secondary part requiring user completion. An unambiguous explicit `is_main` marker is honored; ambiguous/missing designations remain unset. Unsupported schemas or conflicting data fail transactionally for a reviewed migration rather than discarding records. Restoring an older application after schema upgrade may require its matching pre-upgrade database backup.

SQLite uses WAL, full durability, a busy timeout and a single serialized connection. Operations run in a worker thread so disk I/O does not stall Gateway heartbeats. Cancellation waits for a running database operation to finish before releasing its connection lock. A nonblocking filesystem lock refuses a second process against the same path. This is a **single-process design**, not distributed coordination; use local Linux filesystems, not shared/network storage.

Components have stable versioned IDs and `timeout=None`. A global persistent view is registered before READY; it does not depend on message caching. Each component interaction also checks the stored active panel's guild/channel/message and bot ownership. A moved or orphan panel is therefore inactive even if Discord denied the visual cleanup edit.

Panel setup uses a separate per-guild panel lock. It fetches the stored message, verifies ownership and edits it, or recreates it on a confirmed 404. A channel move publishes the new panel, saves it and queues the old ID transactionally, then removes only the old bot panel's controls. It never deletes unrelated messages. Access/API failures do not get mistaken for deletion. Startup refreshes only guilds with saved panel state; it does not publish first-run panels automatically.

There is no atomic transaction across Discord message creation and SQLite. A process/power failure after Discord creates a message but before its ID is committed can leave an orphan panel. Its callbacks fail the active-message check. Restore the database if necessary and remove any orphan manually. Similar role creation interruptions normally recover through unambiguous name discovery. These limits are documented instead of claiming exactly-once external side effects.

## Commands, diagnostics and privacy

The existing configurable class group retains setup/sync/status and compatible legacy member commands. A separately configurable character group supplies add/list/main/edit/remove/help and administrator purge; `/roster` provides same-guild member/class/main-alt filters. Member commands remain available without default group-level administrative restrictions. Each administrative invocation checks the interaction's current Manage Server permission; mutations additionally require Manage Roles. Server membership, guild allowlist, target channel and character ownership are checked at runtime. No member input supplies a role ID.

The public character panel contains a persistent My Characters button, plus Community Roster when guild roster access is enabled. The existing manage/list component IDs remain registered for compatibility with older panels; both open the same dashboard. Admin-only visibility still checks authorization on every roster interaction; a public button does not grant access. Each private view and name modal is bound to the initiating Discord member and guild. Class selection uses native dropdowns and character names use separate modal fields; no manual class spelling or combined-name parsing is required. Planned characters use the same self-reported records and required name fields as existing characters; there is no invented game identity or separate planning state.

The private dashboard separates character selection from mutation: its dropdown chooses the character for Edit, Make Main and Remove. Selection performs no role writes. Editing opens the selected character's modal directly; removal opens confirmation or replacement-main selection directly. The selected character/page survives edits and refreshes, and a new character is selected after registration. Unavailable actions are disabled with explanatory text, including last-main removal policy; service-side checks remain authoritative. Paging, Back and Cancel update that private message rather than accumulating follow-up replies. Community Roster uses native class, main/alt and member filters, paging and a reset control; each action keeps the same guild and visibility checks. Long lists stay below Discord's option/embed limits. Editing/removal choices and slash autocomplete use display names while the service validates opaque IDs against the caller's own roster. Removal and administrator purge require explicit confirmation; a main replacement is chosen before confirming main removal. Public panels are never edited for individual member actions.

Class choices use a shared alphabetical display order based on canonical game names. The legacy class dropdown, name-modal class select, command choices and roster filter use that order. Configured enabled-class order remains available to role reconciliation; stable class IDs, existing role associations and Discord role positions do not change when display ordering changes.

Private interactions acknowledge before waiting for a navigation lock scoped to guild, owner and channel. The same lock serializes opening a replacement menu, in-place navigation and idle cleanup. Only the bot's own ephemeral source messages are edited or deleted. Opening another menu sends the replacement successfully before retiring the old private message; another owner or channel is unaffected. Successfully replacing a view stops its old controls, and queued callbacks/timeouts cannot repaint or delete a newer view. Opening a modal remains immediate. An edit submission includes the character's observed update timestamp, checked under the member lock, so a stale form cannot overwrite a newer edit. Rejected forms retain a draft in memory behind Edit Details; closing/replacing the source menu prevents a late submission from writing.

Every private view has Done and a five-minute idle timeout. Cleanup uses interaction webhook deletion, never a channel-message delete or privileged intent. Accepted interactions renew the deletion token even when they only open a modal; normal navigation binds the new view to its fresh response token. Done deletes the private message without creating another reply. Brief character errors outside the editable form flow use a one-minute deletion delay. Missing messages are tolerated; other cleanup failures log a safe event without tokens or payloads. Discord has no text-channel navigation event, and interaction tokens last 15 minutes, so cleanup is best effort while the bot is running. Views/tokens are not persisted across restarts. See [Discord's interaction lifecycle](https://docs.discord.com/developers/interactions/receiving-and-responding#followup-messages).

The interaction design draws on [GoodBot's documented character-management workflow](https://github.com/aSetback/GoodBot#character-management-commands), particularly native class choices and main/alt roster presentation. This project uses its own implementation; no GoodBot source code was copied. Its scope remains character plans, main/alt management and class colors, without raid scheduling or sign-ups.

Command synchronization hashes the definition and records its scope per application. Startup fetches existing root names; definitions are updated only when needed, and previously stored scope registrations are removed when switching scope. Reconnect events do not trigger command registration. This app owns its entire Discord application's command tree; do not run another command-owning service with the same application.

Status uses current roles and fetches the stored panel, reporting permissions, unmanageable roles, higher-priority colors and actionable warnings. All personal management responses are ephemeral and suppress mentions. Guild roster visibility is independently configurable, never crosses guild boundaries, and never reveals unregistered/inferred characters. Character names and owner IDs are persistent self-reported roster data; no Battle.net credentials, message contents or interaction payloads are collected. Optional webhooks use a bounded queue, fixed host validation, no redirects and finite timeouts. Logs are JSON, redact known secrets and URL/token patterns, and never enable library payload-debug logs.

## Startup checks and readiness

Before Discord connection, startup performs serialized SQLite `quick_check` and foreign-key checks plus a rollback-only write probe. It reports the schema version and verifies the heartbeat file is writable. The probe changes no saved character, role, panel or configuration data. Failed local checks stop startup with safe diagnostics.

After authentication, public bot/application IDs make a token/application mismatch diagnosable without inspecting the token. Command registration and Gateway membership are separate checks: successfully registering global commands does not prove the bot user is installed in a guild. Startup reports no joined guilds, configured guilds missing from the Gateway cache, and joined guilds excluded by the allowlist. A missing bot membership is rejected before interaction-driven role or panel operations; an interaction-provided guild alone is insufficient proof of an installed bot.

Guild preparation retains the existing reconciliation and panel refresh path, then checks current role associations, manageability and the saved panel. No stored panel is normal first-run state and produces setup guidance. Missing permissions, unsafe roles and inaccessible/mismatched saved panels produce per-guild failures and retries. The bot never tests readiness by assigning a member role, fabricating a character, publishing an unsolicited panel or scanning the guild's members.

The heartbeat combines Gateway/command availability with cached guild preparation results. It reports `waiting_for_guild` when no installed guild is eligible, `degraded` when an expected guild is missing or a guild/registration check failed, and `connected` after all required checks pass. Healthy guilds continue operating even while the overall container reports degradation for another guild. `starting`, `disconnected` and stale/unresponsive process states remain distinct. Invites and guild availability events trigger checks after startup; no command re-registration is needed for an ordinary reconnect. The local health command uses the heartbeat file and process identity, never an inbound network listener or a fresh member scan.

HTTP failures log action context, status and Discord's numeric error code, without raw URLs, response bodies or interaction payloads. User-facing diagnostics distinguish missing guild/channel/message from expired interactions rather than interpreting every HTTP 404 as a deleted panel. These checks improve diagnosis but do not replace a real member registration/main-switch test or guarantee external Discord state cannot change after a successful check.

## Extending and testing

Add classes in `classes.py` and keep IDs stable forever. Configuration overrides and role/member services consume that registry without class-specific logic. Existing services receive settings, storage and locks explicitly so tests use a temporary real SQLite store and simulated Discord guild/member/channel APIs. Keep future secondary-class or raid-role behavior in separate policy code rather than broadening role assignment to arbitrary IDs.

Tests exercise state and failure recovery, not a live Discord connection. Real server checks are listed in [releasing.md](releasing.md). Direct host execution is supported on Linux; Windows/macOS use Linux containers. The process lock and signal lifecycle intentionally rely on Linux/POSIX facilities.
