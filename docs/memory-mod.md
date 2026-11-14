# Memory mod

The memory mod puts a Firstmate home's curated memory files into the system prompt of its Claude Code sessions.
This page is for operators enabling it once per machine for a main checkout and its worktree homes.

## Turn it on

In the main checkout's `.claude/settings.local.json`, merge these keys with any existing settings.
Replace `/absolute/path/to/firstmate` with that checkout's absolute path.

```json
{
  "extraKnownMarketplaces": {
    "firstmate-memory": {
      "source": {
        "source": "directory",
        "path": "/absolute/path/to/firstmate/.claude/mods"
      }
    }
  },
  "enabledPlugins": {
    "fm-memory@firstmate-memory": true
  }
}
```

Start a new Claude Code session in a trusted home.
Claude loads local settings from the main checkout in its worktrees too, so every second mate home on that checkout uses the same enable step.
No per-home link or separate plugin install is needed.
Separate clones need their own settings entry.

Function hooks must be available in Claude Code, for example through the operator's existing `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1` setting.
The mod does not check that variable itself or set it for you.
The folder must be trusted: Claude ignores a repository's marketplace declaration before workspace trust, including in a non-interactive `claude -p` run.
See Claude's [marketplace settings](https://code.claude.com/docs/en/settings-reference#extraknownmarketplaces).

To turn the mod off, set `fm-memory@firstmate-memory` to `false` in the same main-checkout file and start a new session.

## Which sessions get memory

The mod adds a section only when the session root holds `data/captain-shared.md`.
It reads all three files from that root's `data/` directory, never from `FM_HOME`, `FM_ROOT_OVERRIDE`, or `FM_DATA_OVERRIDE`.
A home needs only the shared file to qualify.
A worker worktree without it gets no section, even when the mod loads there and the worker inherits the main home's `FM_HOME`.
The mod's installed location does not affect this decision.

The section is named `fm-memory:memory` and appears at the end of the system prompt.
It holds these files in the digest's order:

- `data/captain.md`
- `data/captain-shared.md`
- `data/learnings.md`

A missing file shows `ABSENT` and an empty file shows `(present, empty)`.
A file that cannot be read shows `UNREADABLE` with the reason and its path.
The [startup memory budget](configuration.md#startup-memory-budget-configstartup-memory-budget) still applies.

## What changes in the digest

For a Claude primary, the digest checks the main checkout's local enable setting and the session root's shared-memory marker.
Only when both are present does it leave the three files out of the digest, its read-once contract, and its re-emit cue.
An overridden data directory stays in the digest because the mod does not read it.
Other harnesses, including Pi, keep the memory files in their digest.
`data/projects.md` and `data/secondmates.md` stay in every digest.

The omitted-memory pointer retains the instruction to read the three files directly if the system prompt has no Firstmate memory section.
Settings cannot prove successful loading: an untrusted folder, unavailable function hooks, or a failed plugin load can prevent the section from appearing.

## When the section updates

Claude Code records the system prompt at the conversation's first request and keeps it until compaction.
The section contains the files as they were when the session started, compacted, or cleared with `/clear`.
It does not refresh mid-conversation or on `--continue` or `--resume`.
When the session edits a file during `/stow`, that edit is already in the conversation through the edit itself.
The next compaction or new conversation updates the section.

## Regression entry points

```sh
bin/fm-test-run.sh tests/fm-memory-claude-mod-plugin.test.sh tests/fm-session-start.test.sh
```
