# Memory mod

The memory mod puts a Firstmate home's curated memory files into the system prompt of its Claude Code sessions.
This page is for operators who turn it on for a home and need to know what changes, where it loads, and when its content updates.

## What it does

The mod adds one section, `fm-memory:memory`, at the end of the system prompt.
The section holds three files from the home's data directory, in this order:

- `data/captain.md`
- `data/captain-shared.md`
- `data/learnings.md`

A missing file shows `ABSENT` and an empty file shows `(present, empty)`, as in the session-start digest.

In a home where the mod is on, the session-start digest leaves those three files out.
It prints one line in its CONTEXT section that says the files are in the system prompt.
Its read-once contract and its re-emit cue also leave them out.
`data/projects.md` and `data/secondmates.md` stay in the digest.

The [startup memory budget](configuration.md#startup-memory-budget-configstartup-memory-budget) still applies to the three files.

## Requirements

- The home's primary session runs on Claude Code.
- The session starts with `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1`.
  The mod does nothing without that exact value, and the digest then prints the files as usual.
  Firstmate never sets this flag for you, as [Calm](calm.md#enabling-function-hooks) also describes.
- Claude Code trusts the home's folder.
  Claude Code does not load a mod from the `.claude/skills` folder of an untrusted folder.

Other harnesses, including Pi, do not load the mod, and their digest prints the files as usual.

## Turn it on for one home

Turning the mod on is a separate step for each home.
Run these commands from the home's top folder: the main home, or one second mate home.

```sh
ln -s ../../.claude/mods/firstmate-memory .agents/skills/firstmate-memory
exclude=$(git rev-parse --git-path info/exclude)
grep -qxF .agents/skills/firstmate-memory "$exclude" || printf '%s\n' .agents/skills/firstmate-memory >> "$exclude"
```

The first command makes an untracked link that Claude Code loads through the home's `.claude/skills` folder.
The other two commands keep the link out of `git status`.
The exclude file is shared by every checkout of the repository, so one line is enough for all homes.

The mod loads in the next Claude Code session that starts in the home.

The link is untracked, so other checkouts of the repository do not have it:

- A second mate home needs its own link.
- A worker's copy never gets the link, so a worker never gets the memory section.

To turn the mod off, remove the link.
The next session start prints the three files in the digest again.

### Do not enable it at local scope

Do not enable the mod through `.claude/settings.local.json` or `claude plugin install --scope local`.
In a git worktree, Claude Code reads the local settings of the repository's main checkout.
Every second mate home and every worker on the firstmate repository is a worktree of the main home's checkout, so a local-scope mod in the main home loads in all of them.

## Which sessions get the section

The mod adds its section only when both of these are true:

- `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS` is `1`.
- The mod was loaded from inside the session's root folder.

The second check keeps the section out of a session in any other folder, even if the mod loads there through some other route.
A worker inherits `FM_HOME` naming the main home, so a check on `FM_HOME` alone would not keep it out.

The mod reads the same data directory as the digest: `FM_DATA_OVERRIDE`, else `FM_HOME`, `FM_ROOT_OVERRIDE`, or the session's root folder, followed by `/data`.

The digest leaves the files out when the primary harness is Claude Code, `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS` is `1`, and the home's top folder holds the link.
Its session-open hook runs from the session's root folder, so the digest and the mod check the same folder.

## When the section updates

Claude Code builds a conversation's system prompt at its first request and keeps it until the conversation is compacted.
So the section shows each file's content as it was at one of these points:

- The session started.
- The conversation was compacted, by `/compact` or by Claude Code itself.
- The conversation was cleared with `/clear`.

The section does not change in the middle of a conversation, and it does not change when a conversation is resumed with `claude --continue` or `--resume`.
When a session edits one of the files, for example during `/stow`, the edit is already in the conversation through the edit itself.
The next compaction or new conversation shows the new content in the section.

## Regression entry points

```sh
tests/fm-memory-claude-mod-plugin.test.sh
tests/fm-session-start.test.sh
```
