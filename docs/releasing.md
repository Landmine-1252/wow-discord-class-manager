# Releasing and publishing to GHCR

The workflows publish `ghcr.io/landmine-1252/wow-discord-class-manager` using the repository's built-in `GITHUB_TOKEN`. Ordinary publishing does not need a personal access token. Workflows must be enabled, and repository/organization policy must permit `packages: write` for the publishing job. All other jobs use read-only repository access. Fork PRs run validation without publishing or registry credentials.

## Maintainer procedure

1. Enable private vulnerability reporting under repository Settings → Code security. Protect `main` and release tags, require review/CI, and check the release diff for accidental secrets.
2. Update the version in `pyproject.toml` and `src/wow_class_manager/__init__.py`; update Dockerfile's default VERSION, the image versions in `compose.yaml` and `.env.example`, and relevant documentation. Run `uv lock` so package metadata stays consistent.
3. Run the commands in CONTRIBUTING.md, build the image, and complete the live test below. Verify a restore on a test volume when a migration changes.
4. Merge the reviewed change to `main`. A successful publish workflow builds AMD64 and ARM64 and publishes **`edge` only**. It does not update `latest`.
5. For a stable release, create and push the matching tag after review:

   ```sh
   git tag -a v1.1.0 -m 'Release 1.1.0'
   git push origin v1.1.0
   ```

6. The release workflow first runs CI, checks that a stable `vX.Y.Z` tag matches package metadata, then publishes `1.1.0`, `1.1`, `1`, and `latest`, with OCI source/revision/version labels, provenance and an SBOM. Prerelease tags are intentionally rejected by this workflow. Prefer publishing stable releases in increasing version order because aliases, including `latest`, follow the last stable tag published.
7. Open the package under the GitHub owner's **Packages**, select its **Package settings**, and change visibility to **Public**. **A public repository does not automatically make its GHCR package public.** Confirm the source repository is linked and has Actions access. If a preexisting package denies publishing, adjust its repository access rather than creating a PAT workaround.
8. Verify anonymous pulls of the version tag and both platform manifests. Create GitHub release notes describing configuration/migration changes and any live checks performed. Publish the manifest digest for operators who pin immutable deployments.

The Dockerfile's base images and uv are pinned by multi-platform manifest digest. Dependabot checks Python (`uv`), GitHub Actions and Docker bases weekly. Review these updates, including upstream release changes, and rerun tests/builds. Do not silently substitute an unpinned base in a release. Action SHAs in the workflow are pinned to published release commits.

## Live Discord acceptance test

Use a separate application and test server; this cannot be proven by mocked tests alone.

1. Start with an empty database and no class roles. Invite the app both before startup and, in another test, while it is running. Confirm all nine roles are created once with expected colors, zero permissions, no hoist and no mentionability.
2. Publish `/class setup` once, then rerun it. Confirm one bot-owned panel with the current-plans description, My Characters and the enabled Community Roster button. Test separate required name-entry modal fields and native class choices on desktop and mobile clients. Confirm an explicit `PANEL_DESCRIPTION` override survives an update.
3. As an ordinary member with an unrelated role, register John Stormwind as Paladin, then Sam Ironforge as Warrior. Confirm John becomes main, Sam becomes alt and Paladin remains assigned until Sam is promoted. Select Sam in the dashboard dropdown and choose Make Main, then switch back to John; verify automatic demotion, role/color updates, retained unrelated roles and both records preserved. Test same-class switching. Verify that choosing a character alone never changes roles and Make Main is disabled for the current main. Check that successful add/edit/promote/remove actions refresh the private dashboard, and Back/Cancel/paging navigate without accumulating private messages or changing the public panel. Confirm that Edit opens the selected character directly, a rejected form keeps its draft, and additions/edits retain the selected character and correct page. Inspect compact action rows on mobile.
   Close a menu with Done and leave another idle for five minutes; verify that only the private message disappears. Reopen from the public panel twice and confirm the older private menu closes without affecting another member/channel. Open a form, close its source menu, and verify a late submission cannot write. Repeat with a temporary Discord API failure and confirm saved characters remain intact; older messages after a restart may require manual dismissal.
4. Restart/recreate the container while preserving `/data`. Test the original message. Confirm no new roles or duplicate panel and command registration unchanged.
5. In a second guild, pre-create safe class roles. Confirm adoption. Add duplicate names, an unsafe privileged role and a hierarchy problem; verify clear warnings, no unsafe assignment and continued operation in the other guild.
6. Attempt setup, sync and status as an ordinary member. Confirm refusal; member commands still work. Verify privileged intents remain disabled and no inbound listeners/ports exist.
7. Change a role name, panel text, enabled classes and root command through the environment. Recreate the container and confirm saved role IDs survive, disabled roles/memberships remain, old commands are replaced, and the existing panel updates.
8. Delete the panel and rerun setup. Move it explicitly to another channel and confirm only the old bot panel's controls are removed. Confirm old panel actions are refused even if it cannot be edited.
9. Temporarily interrupt network access, restore it and test reconnection. Send SIGTERM and verify clean shutdown. Review health transitions and redacted logs. Test token-file deployment and optional webhook delivery if using those features.
10. Pull the published image anonymously on AMD64 and ARM64 hosts, follow the README from a clean environment, and test a backup/restore. Record the platform and image digest used.
11. Upgrade a real schema-2 fixture with existing class-only members and the saved panel. Confirm schema 3, preserved IDs/assignments, no fabricated characters and in-place panel refresh. Test supported legacy single-name completion and transaction rollback on incompatible legacy data.
12. Confirm Add/Edit collects both name parts and one **Class & Role** choice, with one Submit to save and the saved pair preselected on Edit. Confirm choices are grouped by class with a leading class icon and the role icon beside its name, and Priest/Tank and Hunter/Healing are absent. Test all 13 classes enabled (25 options) on desktop and mobile. Confirm a rejected name or failed save retains names and the valid pair behind Edit Details. Rename each character name component without changing the character ID. Edit an alt's class (no role change) and a main's class (role change). Test fixed-class role changes, saved disabled classes, duplicate full names, accented names and more than 25 characters with pagination. Confirm dashboard accents follow the selected character and community-roster class filter, including custom class colors, while browsing preserves Discord role assignments, name colors and member-list grouping.
13. Remove an alt with confirmation. Remove a main only after choosing a replacement. Test last-character removal with clearing disabled/enabled. Test confirmed administrator purge for a present and departed user; verify scoped data deletion and unchanged Discord roles.
14. Open Community Roster and `/roster`; test class, main/alt and member dropdowns, combined filters, Reset Filters, paging and an empty result. Verify members/admins/disabled visibility, including denial when an ordinary member clicks the visible admins-only button and hiding the button when disabled. Test attempts to use another member's private components or character IDs. Test the same user's independent rosters in two guilds.
15. Interrupt a main switch between Discord role confirmation and SQLite commit. Verify the prior committed main remains or is restored, pending work is detected after restart, retries are safe and no success is reported prematurely. Remove a role manually, add a duplicate class role and disable a main's class; confirm actionable recovery without arbitrary replacement characters.

Until those live steps and publication are actually performed, describe the project as locally implemented/tested with live integration pending. Do not claim a running deployment or publicly available image based only on workflow files.
