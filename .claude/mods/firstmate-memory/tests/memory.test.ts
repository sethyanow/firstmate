// firstmate-memory under `claude plugin test`: the memory section the mod adds to the
// system prompt, and the sessions it leaves alone.
import type { On, PromptComposeSection } from "claude-code";
import { describe, expect, mock, test, type Engine } from "claude-code/testing";

const ENGINE_SECTIONS: PromptComposeSection[] = [
  { id: "intro", text: "engine intro", scope: "shared" },
  { id: "env_info_simple", text: "engine env", scope: "session" },
];

type WorldOptions = {
  /** The session's project root; omitted means the folder above the mod's own. */
  sessionRoot?: string;
  env?: Record<string, string>;
  /** Function-hooks opt-in value; omitted means the active value `1`. */
  functionHooks?: string | undefined;
};

/** The folder holding the mod's own folder, which a session rooted there loads it from. */
const MOD_PARENT = new URL("../..", (import.meta as ImportMeta & { url: string }).url).pathname.replace(/\/$/, "");

/** What a session composes for; the mod reads none of it. */
const COMPOSE_INPUT = {
  model: "claude-haiku-5-5",
  promptModel: "claude-haiku-5-5",
  surfaces: ["terminal"],
  tools: [],
  outputStyle: null,
  traits: [],
} as const;

/** A session whose files live in `files`, with the engine's own sections beneath the mod. */
function world(on: On, options: WorldOptions = {}) {
  const functionHooks = "functionHooks" in options ? options.functionHooks : "1";
  mock.env(on, {
    ...(options.env ?? {}),
    ...(functionHooks === undefined ? {} : { CLAUDE_CODE_ENABLE_FUNCTION_HOOKS: functionHooks }),
  });
  const root = options.sessionRoot ?? MOD_PARENT;
  const files = new Map<string, string>();
  // Paths that exist but refuse every read.
  const unreadable = new Set<string>();
  const reads: string[] = [];
  on("session.root", async () => ({ value: root }));
  on("fs.exists", async (_$, e) => ({ value: files.has(e.path) || unreadable.has(e.path) }));
  on("fs.read", async (_$, e) => {
    reads.push(e.path);
    return files.has(e.path) ? { value: files.get(e.path)! } : { deny: `EACCES: ${e.path}` };
  });
  on("prompt.compose", async () => ({ sections: ENGINE_SECTIONS }));
  return { root, files, unreadable, reads };
}

async function memoryText($: Engine): Promise<string | undefined> {
  const { sections } = await $.prompt.compose(COMPOSE_INPUT);
  return sections.find((section) => section.id === "fm-memory:memory")?.text;
}

describe("the memory section", () => {
  test("is appended last, on the session side, holding each file under its digest label", async ($, on) => {
    const { root, files } = world(on);
    files.set(`${root}/data/captain.md`, "Prefers short replies.\n");
    files.set(`${root}/data/captain-shared.md`, "Shared rule.\n");
    files.set(`${root}/data/learnings.md`, "Fact one.\n");

    const { sections } = await $.prompt.compose(COMPOSE_INPUT);

    expect(sections.slice(0, 2)).toEqual(ENGINE_SECTIONS);
    expect(sections).toHaveLength(3);
    const memory = sections[2]!;
    expect(memory.id).toBe("fm-memory:memory");
    expect(memory.scope).toBe("session");
    expect(memory.text).toContain('<memory-file name="data/captain.md">\nPrefers short replies.\n</memory-file>');
    expect(memory.text).toContain(
      '<memory-file name="data/captain-shared.md (shared, main-authoritative, read-only in secondmate homes)">\nShared rule.\n</memory-file>',
    );
    expect(memory.text).toContain('<memory-file name="data/learnings.md">\nFact one.\n</memory-file>');
  });

  test("marks a missing file ABSENT and an empty one as present and empty", async ($, on) => {
    const { root, files } = world(on);
    files.set(`${root}/data/captain-shared.md`, "");
    files.set(`${root}/data/learnings.md`, "Fact one.\n");

    const text = await memoryText($);

    expect(text).toContain('<memory-file name="data/captain.md">\nABSENT\n</memory-file>');
    expect(text).toContain("read-only in secondmate homes)\">\n(present, empty)\n</memory-file>");
  });

  test("names a file it could not read and tells the model to read it directly", async ($, on) => {
    const { root, unreadable } = world(on);
    unreadable.add(`${root}/data/learnings.md`);

    const text = await memoryText($);

    expect(text).toContain(`UNREADABLE (`);
    expect(text).toContain(`read ${root}/data/learnings.md directly before acting.`);
  });

  test("reads each file again on every composition", async ($, on) => {
    const { root, files } = world(on);
    files.set(`${root}/data/learnings.md`, "marker-r1\n");
    expect(await memoryText($)).toContain("marker-r1");

    files.set(`${root}/data/learnings.md`, "marker-r2\n");

    const text = await memoryText($);
    expect(text).toContain("marker-r2");
    expect(text).not.toContain("marker-r1");
  });

  test("reads the data directory the digest reads: FM_HOME before the session root", async ($, on) => {
    const { root, files } = world(on, { env: { FM_HOME: "/homes/main" } });
    files.set("/homes/main/data/captain.md", "from FM_HOME\n");
    files.set(`${root}/data/captain.md`, "from the session root\n");

    const text = await memoryText($);

    expect(text).toContain("from FM_HOME");
    expect(text).not.toContain("from the session root");
  });

  test("reads FM_DATA_OVERRIDE before FM_HOME", async ($, on) => {
    const { files } = world(on, { env: { FM_HOME: "/homes/main", FM_DATA_OVERRIDE: "/elsewhere/data" } });
    files.set("/elsewhere/data/captain.md", "from the override\n");
    files.set("/homes/main/data/captain.md", "from FM_HOME\n");

    const text = await memoryText($);

    expect(text).toContain("from the override");
    expect(text).not.toContain("from FM_HOME");
  });
});

describe("sessions the mod leaves alone", () => {
  test("a session whose root does not hold the mod gets no section and no reads", async ($, on) => {
    const { files, reads } = world(on, {
      sessionRoot: "/worktrees/worker",
      env: { FM_HOME: "/homes/main" },
    });
    files.set("/homes/main/data/captain.md", "main home memory\n");

    const { sections } = await $.prompt.compose(COMPOSE_INPUT);

    expect(sections).toEqual(ENGINE_SECTIONS);
    expect(reads).toHaveLength(0);
  });

  for (const value of [undefined, "0", "true"]) {
    test(`is inert when CLAUDE_CODE_ENABLE_FUNCTION_HOOKS is ${value ?? "unset"}`, async ($, on) => {
      const { root, files, reads } = world(on, { functionHooks: value });
      files.set(`${root}/data/captain.md`, "memory\n");

      const { sections } = await $.prompt.compose(COMPOSE_INPUT);

      expect(sections).toEqual(ENGINE_SECTIONS);
      expect(reads).toHaveLength(0);
    });
  }
});
