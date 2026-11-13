// Firstmate memory for Claude Code: the hooks module of the memory mod, whose plugin
// name is `fm-memory`.
//
// One `prompt.compose` hook appends one session-scope section holding the home's three
// curated memory files. Claude Code records a conversation's system prompt at its first
// request and composes it again only after a compaction or a new conversation (`/clear`),
// so each of those starts reads the files' current content. docs/memory-mod.md owns the
// operator contract.
//
// bin/fm-session-start.sh leaves the three files out of its digest when this mod is
// linked into the session's root, so the two must agree on which sessions get the
// section and on which files they hold:
//   - The section is added only when the mod was loaded from inside the session's root.
//     A worker or a second mate home is its own root, so a link in one home never adds
//     memory to another, even when a session inherits FM_HOME naming the main home.
//   - The files are read from the data directory the digest reads: FM_DATA_OVERRIDE,
//     else FM_HOME, FM_ROOT_OVERRIDE, or the session root, followed by `/data`.
//   - A missing file prints ABSENT and an empty one `(present, empty)`, as in the digest.
import type { EngineInterface, PromptComposeSection, Register } from "claude-code";

const SECTION_ID = "fm-memory:memory";

/** The three files, in the digest's order, each with the label the digest prints. */
const MEMORY_FILES = [
  { file: "captain.md", label: "data/captain.md" },
  {
    file: "captain-shared.md",
    label: "data/captain-shared.md (shared, main-authoritative, read-only in secondmate homes)",
  },
  { file: "learnings.md", label: "data/learnings.md" },
] as const;

let activation: Promise<boolean> | undefined;

function isActivated($: EngineInterface): Promise<boolean> {
  if (activation === undefined) {
    activation = $.env.get("CLAUDE_CODE_ENABLE_FUNCTION_HOOKS").then(
      (value) => value === "1",
      () => false,
    );
  }
  return activation;
}

function withoutTrailingSlash(path: string): string {
  return path.length > 1 ? path.replace(/\/+$/, "") : path;
}

async function dataDirectory($: EngineInterface, sessionRoot: string): Promise<string> {
  const override = await $.env.get("FM_DATA_OVERRIDE");
  if (override) return override;
  const home = (await $.env.get("FM_HOME")) || (await $.env.get("FM_ROOT_OVERRIDE")) || sessionRoot;
  return `${withoutTrailingSlash(home)}/data`;
}

async function fileBlock($: EngineInterface, path: string, label: string): Promise<string> {
  let body: string;
  try {
    if (!(await $.fs.exists(path))) {
      body = "ABSENT";
    } else {
      const text = await $.fs.read(path);
      body = text === "" ? "(present, empty)" : text.replace(/\n$/, "");
    }
  } catch (error) {
    body = `UNREADABLE (${error instanceof Error ? error.message : String(error)}) - read ${path} directly before acting.`;
  }
  return `<memory-file name="${label}">\n${body}\n</memory-file>`;
}

async function memorySection($: EngineInterface, data: string): Promise<string> {
  const blocks = await Promise.all(
    MEMORY_FILES.map(({ file, label }) => fileBlock($, `${data}/${file}`, label)),
  );
  return [
    "# Firstmate memory",
    `The curated memory files of this Firstmate home, read from ${data} when this conversation started or was last compacted or cleared.`,
    "A file edited later in this conversation shows its new content here only after the next compaction or new conversation.",
    ...blocks,
  ].join("\n\n");
}

export const register: Register = (on) => {
  on("prompt.compose", async ($, e, next) => {
    const composed = await next(e);
    if (!(await isActivated($))) return composed;
    const sessionRoot = withoutTrailingSlash(await $.session.root());
    if (!$.plugin.root.startsWith(`${sessionRoot}/`)) return composed;
    const text = await memorySection($, await dataDirectory($, sessionRoot));
    const section: PromptComposeSection = { id: SECTION_ID, text, scope: "session" };
    return { sections: [...composed.sections, section] };
  });
};
