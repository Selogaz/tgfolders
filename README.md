# tgfolders

Sort your Telegram groups and channels into chat folders with plain keyword rules.

You write a small JSON config ("chats whose title has a word starting with `job` go to **Jobs**"),
run `tgfolders plan` to see exactly what would change, then `tgfolders apply`. Folders you mark as
protected are never touched.

```
$ tgfolders plan -q
delete folder: Old (id 4; chats stay)
[Jobs] (new): +28
    + Python Jobs (channel, -1001000000001)
    + IT Recruiters (group, -1001000000002)
    ...
[Books] (new): +22
    ...
already in a folder, left alone: 17
no rule matched: 112
```

## Why

Telegram has no rule-based folders: you either add chats by hand or use broad filters like
"all groups". With hundreds of channels that does not scale. tgfolders gives you:

- **A dry run first.** `plan` only reads; `apply` shows the same plan and asks before writing.
- **Protected folders.** List folder names under `protected` and tgfolders refuses any plan that
  would modify or delete them. `init` protects every folder you already have.
- **Add-only by default.** Chats are never removed from a folder. Existing folder settings
  (emoji, pinned chats, "all contacts"-style rules) are kept.
- **Your account's real limits.** Folder and per-folder chat limits are read from Telegram for
  your account type (10 folders / 100 chats without Premium, 30 / 200 with it), and an
  over-limit plan is rejected as a whole.
- **No AI, no cloud.** Rules are deterministic, and chat titles never leave your machine.
- **Clean removal of shared folders.** A folder joined via an invite link can be removed without
  leaving any of its chats.

## Install

Requires Python 3.10+.

```sh
pipx install git+https://github.com/Selogaz/tgfolders
# or: uv tool install git+https://github.com/Selogaz/tgfolders
```

## Sign in

tgfolders works as **your own Telegram client** (via [Telethon](https://github.com/LonamiWebs/Telethon)),
not as a bot: bots cannot see or change your folders. You need an `api_id` and `api_hash` from
<https://my.telegram.org> → *API development tools*.

```sh
tgfolders login          # phone number + code (+ 2FA password if set)
tgfolders login --qr     # if the code does not arrive: scan a QR in Telegram → Settings → Devices
tgfolders status
```

Run `login` in a normal terminal. The session is stored in your system keyring (Secret Service,
macOS Keychain, Windows Credential Locker). If no keyring is available, or `TGFOLDERS_SESSION_FILE`
is set, it goes to a file readable only by you. **A session is full access to your account:**
`tgfolders logout` ends it on the server and deletes it locally.

## Use

```sh
tgfolders init      # writes ~/.config/tgfolders/config.json; all current folders are protected
tgfolders show      # your folders and every group/channel with its id and folder
# edit the config
tgfolders plan      # what would change (read-only)
tgfolders apply     # same plan, then asks for confirmation (-y to skip)
```

## Config

```json
{
  "protected": ["Personal", "Work"],
  "delete": ["Old stuff"],
  "include_sorted": false,
  "folders": [
    {"title": "Jobs",  "emoticon": "💼", "keywords": ["job", "hiring", "вакансии"]},
    {"title": "Books", "emoticon": "📚", "keywords": ["book", "книг", "library"]},
    {"title": "AI",    "keywords": ["ai", "llm", "gpt"], "chats": [-1001234567890]}
  ]
}
```

| Key | Meaning |
|---|---|
| `protected` | Folder names that are never modified or deleted. Matching ignores case, emoji and spaces. |
| `delete` | Folders to delete. Their chats stay in your account and count as unsorted for this run. |
| `include_sorted` | `false` (default): chats already in some folder are skipped by keyword rules. |
| `folders[].title` | Folder name, up to 12 characters. Created if missing, otherwise added to. |
| `folders[].keywords` | A chat matches if any word of its title **starts with** a keyword, ignoring case: `ai` matches "AI-Magic", not "Daily". |
| `folders[].chats` | Explicit chat ids (from `show`). They win over keywords and also apply to already sorted chats. |
| `folders[].emoticon` | Emoji for a new folder. |

Rules are checked top to bottom, and the first matching folder wins, so put specific folders
before broad ones. Keys starting with `_` are ignored and can be used as comments.

## Using with an AI agent

You can let a coding agent (Claude Code, Codex, Cursor, …) draft rules for you, but some of the
guarantees above only hold while a person stays in the loop:

- **Sign in yourself.** `tgfolders login` refuses to run without an interactive terminal, so the
  login code and 2FA password never pass through an agent. Run it in your own terminal.
- **Chat titles reach the agent's provider.** Whatever `show` and `plan` print (chat titles, ids,
  folder names) lands in the agent's context and is sent to its model provider. Message texts
  are never read by tgfolders, so they cannot leak this way. "No chat data leaving your machine"
  does not hold once an agent reads the output.
- **Read the plan yourself.** An agent cannot answer the `[y/N]` prompt, so it will reach for
  `apply --yes`, and then nobody reads the plan. `--yes` is deliberately allowed without a
  terminal (for cron and scripts) and only prints a notice, so this is on you: let the agent run
  `plan`, read it, then run `apply` yourself.
- **Watch the `protected` list.** tgfolders enforces whatever the config says. An agent editing
  the config can also edit `protected`, so check that diff.
- **The session is full account access.** Any program running as your user, an agent with
  shell access included, can read it from the keyring or the session file. That is true of every
  Telegram client. Run `tgfolders logout` when you are done if this matters to you.
- **Lock it down in the agent's permissions.** If your agent supports deny rules, keep `show` and
  `plan` available and deny the commands that change your account. For Claude Code, in
  `~/.claude/settings.json`:

  ```json
  { "permissions": { "deny": ["Bash(tgfolders apply:*)", "Bash(tgfolders login:*)", "Bash(tgfolders logout:*)"] } }
  ```

  Such rules match the command text, so they guard against mistakes, not against an agent set on
  getting around them (`python -m tgfolders apply` is a different string).

## Limits and caveats

- Telegram's own limits apply: a folder lists at most 100 chats explicitly (200 with Premium).
  tgfolders does not split overflowing folders. It stops and tells you.
- New groups are not sorted automatically: run `apply` again (it only adds what is new).
- This uses the official client API with a few requests per run, which is low risk. Still, it acts
  on your real account: read the plan before you apply it.

## Alternatives

- [telefold](https://github.com/solarhell/telefold) and
  [TelegramChatOrganizer](https://github.com/MiliScripts/TelegramChatOrganizer) let an LLM pick
  the folders (any OpenAI-compatible API and Gemini, respectively). They send chat titles,
  descriptions and sample messages to that service. Use them if you'd rather not write rules.
- tgfolders is for when you want predictable, reviewable results from rules you control, and no
  chat data leaving your machine.

## Development

```sh
uv run pytest
```

Tests use a fake client and real Telethon types. They never touch the network or your keyring.

## License

MIT
