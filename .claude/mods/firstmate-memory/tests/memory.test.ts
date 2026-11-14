// firstmate-memory under `claude plugin test`: the memory section the mod adds to the
// system prompt, and the sessions it leaves alone.
import type { On, PromptComposeSection } from "claude-code";
import { describe, expect, mock, test, type Engine } from "claude-code/testing";

const ENGINE_SECTIONS: PromptComposeSection[] = [
  { id: "intro", text: "engine intro", scope: "shared" },
  { id: "env_info_simple", text: "engine env", scope: "session" },
];

type WorldOptions = {
  sessionRoot?: string;
  env?: Record<string, string>;
};

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
  mock.env(on, options.env ?? {});
  const root = options.sessionRoot ?? "/homes/mate";
  const files = new Map<string, string>();
  // Paths that exist but refuse every read.
  const unreadable = new Set<string>();
  on("session.root", async () => ({ value: root }));
  on("fs.exists", async (_$, e) => ({ value: files.has(e.path) || unreadable.has(e.path) }));
  on("fs.read", async (_$, e) => {
    return files.has(e.path) ? { value: files.get(e.path)! } : { deny: `EACCES: ${e.path}` };
  });
  on("prompt.compose", async () => ({ sections: ENGINE_SECTIONS }));
  return { root, files, unreadable };
}

async function memoryText($: Engine): Promise<string | undefined> {
  const { sections } = await $.prompt.compose(COMPOSE_INPUT);
  return sections.find((section) => section.id === "fm-memory:memory")?.text;
}

const LABELS = ["data/captain.md", "data/captain-shared.md", "data/learnings.md"] as const;

/** The part of the section from one file's label up to the next file's label. */
function partFor(text: string, label: (typeof LABELS)[number]): string {
  const start = text.indexOf(label);
  if (start < 0) return "";
  const later = LABELS.map((other) => text.indexOf(other, start + label.length)).filter((at) => at > start);
  return text.slice(start, later.length > 0 ? Math.min(...later) : undefined);
}

describe("the memory section", () => {
  test("is appended last, on the session side, holding each file under its label in the digest's order", async ($, on) => {
    const { root, files } = world(on, { sessionRoot: "/homes/mate" });
    files.set(`${root}/data/captain.md`, "Prefers short replies.\n");
    files.set(`${root}/data/captain-shared.md`, "Shared rule.\n");
    files.set(`${root}/data/learnings.md`, "Fact one.\n");

    const { sections } = await $.prompt.compose(COMPOSE_INPUT);

    expect(sections.slice(0, 2)).toEqual(ENGINE_SECTIONS);
    expect(sections).toHaveLength(3);
    const memory = sections[2]!;
    expect(memory.id).toBe("fm-memory:memory");
    expect(memory.scope).toBe("session");
    expect(partFor(memory.text, "data/captain.md")).toContain("Prefers short replies.");
    expect(partFor(memory.text, "data/captain-shared.md")).toContain("Shared rule.");
    expect(partFor(memory.text, "data/learnings.md")).toContain("Fact one.");
    const order = LABELS.map((label) => memory.text.indexOf(label));
    expect(order).toEqual([...order].sort((a, b) => a - b));
  });

  test("marks a missing file ABSENT and an empty one as present and empty", async ($, on) => {
    const { root, files } = world(on);
    files.set(`${root}/data/captain-shared.md`, "");
    files.set(`${root}/data/learnings.md`, "Fact one.\n");

    const text = await memoryText($);

    expect(partFor(text!, "data/captain.md")).toContain("ABSENT");
    expect(partFor(text!, "data/captain-shared.md")).toContain("(present, empty)");
    expect(partFor(text!, "data/learnings.md")).not.toContain("ABSENT");
  });

  test("marks a file it could not read UNREADABLE and names its path", async ($, on) => {
    const { root, files, unreadable } = world(on);
    files.set(`${root}/data/captain-shared.md`, "Shared rule.");
    unreadable.add(`${root}/data/learnings.md`);

    const text = await memoryText($);

    expect(partFor(text!, "data/learnings.md")).toContain("UNREADABLE");
    expect(text).toContain(`${root}/data/learnings.md`);
  });

  test("reads each file again on every composition", async ($, on) => {
    const { root, files } = world(on);
    files.set(`${root}/data/captain-shared.md`, "Shared rule.");
    files.set(`${root}/data/learnings.md`, "marker-r1\n");
    expect(await memoryText($)).toContain("marker-r1");

    files.set(`${root}/data/learnings.md`, "marker-r2\n");

    const text = await memoryText($);
    expect(text).toContain("marker-r2");
    expect(text).not.toContain("marker-r1");
  });

  test("uses only shared memory to identify a home, even without the function-hooks environment flag", async ($, on) => {
    const { root, files } = world(on);
    files.set(`${root}/data/captain-shared.md`, "Only shared memory.");

    const text = await memoryText($);

    expect(partFor(text!, "data/captain-shared.md")).toContain("Only shared memory.");
    expect(partFor(text!, "data/captain.md")).toContain("ABSENT");
    expect(partFor(text!, "data/learnings.md")).toContain("ABSENT");
  });

  test("ignores inherited home, root, and data paths", async ($, on) => {
    const { root, files } = world(on, { env: {
      FM_HOME: "/homes/main",
      FM_ROOT_OVERRIDE: "/homes/other",
      FM_DATA_OVERRIDE: "/elsewhere/data",
    } });
    files.set(`${root}/data/captain-shared.md`, "This home's shared memory.");
    files.set(`${root}/data/captain.md`, "This home's preferences.");
    files.set(`${root}/data/learnings.md`, "This home's learnings.");
    for (const directory of ["/homes/main/data", "/homes/other/data", "/elsewhere/data"]) {
      for (const file of ["captain.md", "captain-shared.md", "learnings.md"]) {
        files.set(`${directory}/${file}`, "Foreign memory.");
      }
    }

    const text = await memoryText($);

    expect(text).toContain("This home's shared memory.");
    expect(text).toContain("This home's preferences.");
    expect(text).toContain("This home's learnings.");
    expect(text).not.toContain("Foreign memory.");
  });
});

describe("sessions the mod leaves alone", () => {
  test("a worker without shared memory stays out even with inherited home paths", async ($, on) => {
    const { files } = world(on, {
      sessionRoot: "/worktrees/worker",
      env: { FM_HOME: "/homes/main", FM_ROOT_OVERRIDE: "/homes/main", FM_DATA_OVERRIDE: "/homes/main/data" },
    });
    files.set("/homes/main/data/captain-shared.md", "Main home's marker.");
    files.set("/homes/main/data/captain.md", "Main home's preferences.");
    files.set("/worktrees/worker/data/captain.md", "A captain file alone is not a home marker.");

    const { sections } = await $.prompt.compose(COMPOSE_INPUT);

    expect(sections).toEqual(ENGINE_SECTIONS);
  });
});
