# Set up your Discord application

Every installation uses its own application and token. You do not need Python knowledge or a publicly reachable server.

## 1. Create the application and token

1. Sign in to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Select **New Application**, enter a name such as “Guild Classes”, accept the terms and create it.
3. Open **Bot** in the application sidebar. New applications already have a bot user. If an older application offers **Add Bot**, enable the bot there.
4. Under **Token**, choose **Reset Token**, complete any authentication and copy the new token. Resetting a token immediately invalidates the previous token. Save it in a password manager or directly in your private token configuration; it is not shown again.
5. Put the token in `DISCORD_BOT_TOKEN` in `.env`, or use the [mounted-secret setup](deployment.md#docker-secrets). Do not include a `Bot ` prefix.

The **Application ID** is a public identifier, not a token. This bot normally discovers it automatically. The **Public Key** is not needed.

Leave **Interactions Endpoint URL unset**. This application receives interactions over its outbound Gateway connection. Do not copy the public-HTTP-endpoint steps from tutorials for webhook-based apps. Discord documents both delivery methods in [Receiving and Responding to Interactions](https://docs.discord.com/developers/interactions/receiving-and-responding).

### Optional: project artwork

<img src="assets/branding/cmb-icon.png" alt="Class Manager Bot icon: a cyan star inside a multicolored ring" width="128" height="128">

Download the [square icon PNG](assets/branding/cmb-icon.png) for your application and the [wide banner PNG](assets/branding/cmb-banner.png) for community documentation. On GitHub, open either file and use **Download raw file** to save the original image.

To set the application icon, open your application in the Developer Portal, choose **General Information**, upload the icon and save your changes. This is the application metadata page described in [Discord's setup guide](https://docs.discord.com/developers/quick-start/getting-started#step-1-creating-an-app). Preview the result in Discord after saving. Each operator can choose their own artwork.

## 2. Configure installation

Open **Installation** in the sidebar:

1. Under **Installation Contexts**, enable **Guild Install**. User Install is not used by this project and can be disabled.
2. Under **Install Link**, select **Discord Provided Link**.
3. Under **Default Install Settings → Guild Install**, add the `bot` and `applications.commands` scopes.
4. Select these bot permissions:

   | Permission | Why it is needed |
   | --- | --- |
   | Manage Roles | Create, color, rename and assign safe class roles below the bot |
   | View Channels | Access the selection channel |
   | Send Messages | Publish the panel |
   | Embed Links | Display the clean selection embed |
   | Read Message History | Fetch the stored bot panel for reuse and diagnostics |

5. Save changes and copy the provided installation URL.

Do **not** select Administrator, Manage Server, Manage Messages, Mention Everyone or any additional privileged permission for the bot. Editing its own panel does not require Manage Messages. An officer invoking setup needs Manage Server and Manage Roles; those are separate from the bot's permissions.

Under **Bot → Privileged Gateway Intents**, leave **Presence Intent**, **Server Members Intent** and **Message Content Intent** disabled. The application uses only the nonprivileged Guilds intent plus interaction data and individual member lookups.

The portal's [official installation guide](https://github.com/discord/discord-api-docs/blob/main/developers/quick-start/getting-started.mdx) describes the current Installation controls; its sample app's HTTP endpoint and public-key requirements do not apply here.

## 3. Invite and arrange roles

Open the installation link, choose **Add to Server**, select your server and authorize. Your account needs permission to manage that server. You can invite before or after starting the container. When the bot first connects or joins, it reconciles the enabled class roles automatically.

Verify the bot appears under **Server Settings → Members**. Installing the app only on your own account, or authorizing commands without the `bot` scope, does not add the bot member needed to manage roles. `commands_ready` confirms command registration, while `gateway_ready guild_count=0` means there were no connected servers at that startup snapshot. If you invited after that line, look for a later `guild_ready` or restart once to check the fresh count. See [connection troubleshooting](troubleshooting.md#connected-to-discord-but-guild_count0).

In Discord, open **Server Settings → Roles**. Drag the bot's highest role above all class roles. Manage Roles only allows the bot to manage roles below its highest role; granting the permission alone cannot bypass the hierarchy. The bot never reorders anything itself.

Existing roles are reused by case-insensitive name only when there is one match, no assigned permissions, no channel override granting permissions, and the role is below the bot. Integration-managed roles are rejected. For conflicts, rename unwanted roles or choose distinct configured names, then run `/class sync`. Never remove security permissions from a real officer role just to make it adoptable; use a separate cosmetic class role.

Discord uses a member's highest colored role for their displayed name. If officer colors should win, leave those roles higher. If class colors should win, an administrator can deliberately move class roles above the officer-colored roles, while keeping the bot above the class roles. This changes color priority, not permission inheritance.

## 4. Publish and test

1. Create a normal server text channel, for example `#guild-characters`. A forum, thread, voice channel or DM is not a supported panel destination.
2. Let intended members view the channel and use application commands. You can make it read-only for ordinary chat; members can still use native components. Ensure the bot has all five relevant permissions described above (Manage Roles at server scope and the four channel permissions in this channel).
3. Start the container using the [deployment guide](deployment.md). Check its startup diagnostics, `commands_ready`, and `guild_ready` for your server. An unconfigured panel is expected before the first setup command; missing roles or permissions need attention.
4. With Manage Server and Manage Roles, type `/class setup` in that channel. Alternatively use `/class setup channel:#guild-characters`. The same setup command publishes **World of Warcraft Character Manager**, with **My Characters** and, when enabled, **Community Roster** buttons. Rerunning setup updates the saved bot message.
5. Check the private setup summary for warnings. Run `/class status` if anything is missing.
6. Test with an ordinary member account: click **My Characters → Add Character**, choose **Paladin**, and enter **John** and **Stormwind** in the separate First Name and Secondary Name fields. John Stormwind becomes main and receives the Paladin role. Add **Sam Ironforge** as **Warrior**; Sam becomes an alt and Paladin remains assigned. Select Sam in the character dropdown, choose **Make Main**, and confirm Warrior replaces Paladin while John automatically becomes an alt. The private dashboard refreshes after each change; unrelated roles must stay.
7. Restart the container and test the **same panel** again. No replacement panel or duplicate roles should appear.

If your operator changes command names, replace `/class` and the subcommand names in these examples with the configured names.

Administrators do not need a registered character to run setup. `/class setup` publishes the shared panel; `/character add` registers a personal character. A request to change, edit or remove a character before registering one returns you to the first-character workflow.

## 5. Member self-service

Members can manage their own characters without an officer:

| Action | Button workflow | Slash alternative |
| --- | --- | --- |
| Register a character | My Characters → Add Character → class → separate name fields | `/character add` |
| View main and alts | My Characters | `/character list` |
| Promote an alt | My Characters → choose character → Make Main | `/character main` |
| Rename or change class | My Characters → choose character → Edit | `/character edit` |
| Remove a character | My Characters → choose character → Remove → confirm | `/character remove` |
| View guild characters | Community Roster → class, main/alt or member filter | `/roster` |

Main selection is one operation: the old main automatically becomes an alt. Selecting John Stormwind again switches back without recreating either record. Editing Sam Ironforge while Sam is an alt does not assign another class role. Editing the main's class changes its Discord role after validation. Names can be edited independently; the internal character ID and main/alt designation stay the same.

Class choices are alphabetical by their game names in dropdowns, character forms and slash commands. The default choices are Druid, Hunter, Mage, Paladin, Priest, Rogue, Shaman, Warlock and Warrior. Custom Discord role names do not change this order; optional modern classes join the same alphabetical list when enabled.

Members can share characters they plan to play, even before creating them in game. Both name parts remain required; enter the intended names and edit them when plans change. No record is fabricated from a Discord name or class role, and registration does not verify game ownership or name availability.

Removing a main while alts exist asks for a replacement main; the old character is removed only after that change succeeds. Removing the last character is controlled by `ALLOW_CLEAR_SELECTION`. Members with more than one page of characters can use pagination instead of deleting alts to fit a dropdown.

Navigation updates the existing private message. Use Back to return to the dashboard and Cancel to leave a removal confirmation without changing characters. Successful additions, edits, main changes and removals show an updated dashboard with a private confirmation. Choosing a character alone never changes your main. Rejected forms keep the entered names/class behind Edit Details for correction. The public panel stays the same for everyone.

Choose Done when finished to close the private message. Inactive menus also close after five minutes, and opening another menu in the same channel removes your previous one. The bot cannot detect when you switch text channels. After a restart or failed cleanup, use Discord's Dismiss message action for an older menu.

Community Roster lists only self-reported registered characters in the current server. Change the class, main/alt and member filters using dropdowns; use Previous/Next for pages and Reset Filters to show everyone again. Operators can restrict access to Manage Server holders or disable it; private personal rosters remain available. When access is restricted to admins, the public button remains visible but refuses unauthorized requests. Members cannot edit another member's characters. `/character purge` is an officer-only confirmed deletion workflow for a user's stored character data, including departed users; it leaves their Discord roles as a legacy selection.

When upgrading an existing installation, members with only a class role will see guidance such as “You currently have Paladin selected. Register your main character to begin managing your characters and alts.” Their role remains unchanged until they register. No game character names are inferred from Discord names or roles. If an imported legacy character needs its second name, select it and use Edit to complete both fields rather than creating a duplicate.

With `ENABLE_CHARACTER_MANAGEMENT=false`, setup retains the original class dropdown. Existing character records stay stored; re-enable the feature to manage them again.

## Commands are not visible

Confirm the bot is online, installed in the server, and the install link includes `applications.commands`. Check `commands_ready` or `command_registration_failed` in the logs. Global registration can take time to appear and clients may need a reload; guild-scoped commands normally appear quickly. For testing, use both `COMMAND_SYNC_SCOPE=guild` and your `DEVELOPMENT_GUILD_ID`.

Check the server's **Integrations** settings and the member's **Use Application Commands** permission. Commands are server-only. No Message Content intent is necessary. Keep the Interactions Endpoint URL empty even if commands are missing. See [troubleshooting](troubleshooting.md) for further checks.
