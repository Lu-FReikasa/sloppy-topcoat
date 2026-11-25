/** Editing a block program as data (PRG-00 R4): where a block lives, how it moves, what a variable
 *  may be at a place, and the typing rules the hub's validator applies (`blocks/program.py`).
 *
 *  Everything here is pure: a program goes in, a changed copy comes out. The hub stays the
 *  authority (the "Check" button asks it); this module only lets the editor refuse the moves that
 *  would read a variable before it exists or with the wrong type, so a drag that cannot work does
 *  not snap into place. */

export type VarType = "text" | "texts" | "verdict" | "compare" | "bool";
export type Block = Record<string, unknown> & { b: string };
export interface Program {
  name?: string;
  title?: string;
  inputs: Record<string, string>;
  stack: Block[];
  [key: string]: unknown;
}

/** Where a stack of blocks lives: `[]` is the program's own; `[2, "then"]` the `then` of the third
 *  block; `[1, "lanes", 0]` the first lane of the second block. */
export type StackPath = (number | string)[];

export interface Issue {
  /** like `stack[2].then[0].draft` */
  path: string;
  message: string;
}

export class EditError extends Error {}

const STACK_KEYS = ["then", "else", "stack"] as const;

/** The block kinds and the fields of each, as the hub's catalogue lists them. */
export const KINDS: Record<string, { required: string[]; optional: string[] }> = {
  ask: { required: ["prompt", "out"], optional: ["system", "pick", "max_tokens", "temperature"] },
  agent: { required: ["prompt", "out"], optional: ["app", "depth", "pick", "hooks"] },
  check: { required: ["in", "out"], optional: ["question", "pick"] },
  revise: { required: ["draft", "out"], optional: ["issues", "instruction", "pick"] },
  set: { required: ["out", "value"], optional: [] },
  if: { required: ["cond", "then"], optional: ["else"] },
  repeat_until: { required: ["cond", "stack"], optional: ["guard", "max"] },
  parallel: { required: ["lanes"], optional: ["max_parallel"] },
  compare: { required: ["in", "out"], optional: ["mode", "rule", "pick"] },
  for_each: { required: ["in", "as", "stack"], optional: [] },
  stop: { required: [], optional: ["reason"] },
  file_report: { required: ["in"], optional: ["title"] },
};

/** The types a variable field accepts; `null` = the field is no variable. */
export function fieldTypes(kind: string, field: string): VarType[] | null {
  const table: Record<string, Record<string, VarType[]>> = {
    check: { in: ["text"], question: ["text"] },
    revise: { draft: ["text"], issues: ["verdict", "compare"] },
    compare: { in: ["text", "verdict"] },
    for_each: { in: ["texts"] },
    file_report: { in: ["text"] },
  };
  return table[kind]?.[field] ?? null;
}

const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

// --- places -----------------------------------------------------------------------------------

/** The stack at `path`, or undefined when there is none. */
export function stackAt(program: Program, path: StackPath): Block[] | undefined {
  let stack: Block[] | undefined = program.stack;
  let i = 0;
  while (i < path.length) {
    const block: Block | undefined = stack?.[path[i] as number];
    if (!block) return undefined;
    const key = path[i + 1] as string;
    if (key === "lanes") {
      const lanes = block.lanes as Block[][] | undefined;
      stack = lanes?.[path[i + 2] as number];
      i += 3;
    } else {
      stack = block[key] as Block[] | undefined;
      i += 2;
    }
    if (!Array.isArray(stack)) return undefined;
  }
  return stack;
}

function mustStackAt(program: Program, path: StackPath): Block[] {
  const stack = stackAt(program, path);
  if (!stack) throw new EditError("There is no such place in the program.");
  return stack;
}

/** The stacks a block holds, with the path to each. */
export function childPaths(block: Block, at: StackPath, index: number): StackPath[] {
  const out: StackPath[] = [];
  for (const key of STACK_KEYS) {
    if (Array.isArray(block[key])) out.push([...at, index, key]);
  }
  if (Array.isArray(block.lanes)) {
    (block.lanes as unknown[]).forEach((_, lane) => out.push([...at, index, "lanes", lane]));
  }
  return out;
}

// --- the typing rules of the hub ----------------------------------------------------------------

const REF = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)/g;

/** The variables a text template names. */
export function references(text: string): string[] {
  return [...text.matchAll(REF)].map((m) => m[1] ?? "");
}

type Env = Record<string, VarType>;
type Probe = (path: StackPath, index: number, env: Env) => void;

const isName = (value: unknown): value is string =>
  typeof value === "string" && /^[A-Za-z_][A-Za-z0-9_]*$/.test(value) && value.length <= 40;

/** The variables a stack writes, in order (nested stacks included). */
export function written(stack: Block[]): string[] {
  const out: string[] = [];
  const walk = (blocks: Block[]) => {
    for (const b of blocks) {
      if (typeof b.out === "string" && !out.includes(b.out)) out.push(b.out);
      for (const key of STACK_KEYS) if (Array.isArray(b[key])) walk(b[key] as Block[]);
      if (Array.isArray(b.lanes)) for (const lane of b.lanes as Block[][]) walk(lane);
    }
  };
  walk(stack);
  return out;
}

class Checker {
  issues: Issue[] = [];
  constructor(private probe?: Probe) {}

  bad(path: string, message: string) {
    this.issues.push({ path, message });
  }

  want(env: Env, name: unknown, types: VarType[], path: string) {
    if (!isName(name)) return this.bad(path, `${String(name)} is not a variable name`);
    const have = env[name];
    if (!have) return this.bad(path, `${name} is not defined before this block`);
    if (!types.includes(have)) {
      this.bad(path, `${name} is ${have}, this block needs ${types.join("/")}`);
    }
  }

  template(env: Env, text: unknown, path: string) {
    if (typeof text !== "string" || !text.trim()) return this.bad(path, "text is required");
    for (const ref of references(text)) {
      if (!env[ref]) this.bad(path, `{{${ref}}} is not defined before this block`);
    }
  }

  pick(env: Env, block: Block, path: string) {
    const pick = block.pick as Record<string, unknown> | undefined;
    if (!pick) return;
    if (pick.same_as !== undefined) {
      this.want(env, pick.same_as, ["text", "verdict", "compare"], `${path}.pick.same_as`);
    }
    for (const v of (pick.other_than as unknown[]) ?? []) {
      this.want(env, v, ["text", "verdict", "compare"], `${path}.pick.other_than`);
    }
  }

  cond(env: Env, cond: unknown, path: string) {
    if (typeof cond !== "string" || !cond.trim()) return this.bad(path, "cond is required");
    let text = cond.trim();
    if (text.startsWith("not ")) text = text.slice(4).trim();
    const [variable, field = ""] = text.split(".");
    const have = env[variable ?? ""];
    if (!have || !["bool", "verdict", "compare"].includes(have)) {
      return this.bad(path, `${variable} is not a bool, verdict or compare defined before this block`);
    }
    const wanted = { bool: "", verdict: "ok", compare: "agree" }[have as "bool" | "verdict" | "compare"];
    if (field !== wanted) this.bad(path, `use ${variable}${wanted ? "." + wanted : ""} here`);
  }

  stack(blocks: unknown, env: Env, at: StackPath, label: string): Env {
    let current = { ...env };
    if (!Array.isArray(blocks)) {
      this.bad(label, "a stack is a list of blocks");
      return current;
    }
    blocks.forEach((raw, i) => {
      this.probe?.(at, i, current);
      current = this.block(raw as Block, current, at, i, `${label}[${i}]`);
    });
    this.probe?.(at, blocks.length, current);
    return current;
  }

  block(b: Block, envIn: Env, at: StackPath, index: number, path: string): Env {
    const env = { ...envIn };
    const spec = KINDS[b.b];
    if (!spec) {
      this.bad(path, `unknown block ${String(b.b)}`);
      return env;
    }
    for (const field of spec.required) {
      if (!(field in b)) this.bad(path, `${b.b} needs ${field}`);
    }
    const out = () => {
      if (isName(b.out)) env[b.out] = "text";
      else if ("out" in b) this.bad(`${path}.out`, `${String(b.out)} is not a variable name`);
    };
    const setOut = (type: VarType) => {
      if (isName(b.out)) env[b.out] = type;
      else if ("out" in b) this.bad(`${path}.out`, `${String(b.out)} is not a variable name`);
    };
    switch (b.b) {
      case "ask":
        this.template(envIn, b.prompt, `${path}.prompt`);
        if ("system" in b) this.template(envIn, b.system, `${path}.system`);
        this.pick(envIn, b, path);
        out();
        break;
      case "agent":
        this.template(envIn, b.prompt, `${path}.prompt`);
        this.pick(envIn, b, path);
        out();
        break;
      case "check":
        this.want(envIn, b.in, ["text"], `${path}.in`);
        if ("question" in b) this.want(envIn, b.question, ["text"], `${path}.question`);
        this.pick(envIn, b, path);
        setOut("verdict");
        break;
      case "revise":
        this.want(envIn, b.draft, ["text"], `${path}.draft`);
        if ("issues" in b) this.want(envIn, b.issues, ["verdict", "compare"], `${path}.issues`);
        if ("instruction" in b) this.template(envIn, b.instruction, `${path}.instruction`);
        this.pick(envIn, b, path);
        out();
        break;
      case "set":
        this.template(envIn, b.value, `${path}.value`);
        out();
        break;
      case "if": {
        this.cond(envIn, b.cond, `${path}.cond`);
        const a = this.stack(b.then, envIn, [...at, index, "then"], `${path}.then`);
        const c = this.stack(b.else ?? [], envIn, [...at, index, "else"], `${path}.else`);
        for (const [k, v] of Object.entries(a)) {
          if (c[k] === v && env[k] !== v) env[k] = v;
        }
        break;
      }
      case "repeat_until": {
        this.cond(envIn, b.cond, `${path}.cond`);
        const after = this.stack(b.stack, envIn, [...at, index, "stack"], `${path}.stack`);
        Object.assign(env, after);
        if (Array.isArray(b.stack) && written(b.stack as Block[]).length === 0) {
          this.bad(path, "the body of a loop must write a variable");
        }
        break;
      }
      case "parallel": {
        const lanes = b.lanes as Block[][] | undefined;
        if (!Array.isArray(lanes) || lanes.length < 2 || lanes.length > 8) {
          this.bad(`${path}.lanes`, "two to eight lanes");
          break;
        }
        const seen = new Map<string, number>();
        lanes.forEach((lane, li) => {
          const laneEnv = this.stack(lane, envIn, [...at, index, "lanes", li], `${path}.lanes[${li}]`);
          for (const v of written(lane)) {
            if (seen.has(v)) this.bad(path, `lanes ${seen.get(v)} and ${li} both write ${v}`);
            seen.set(v, li);
            const type = laneEnv[v];
            if (type) env[v] = type;
          }
        });
        break;
      }
      case "compare": {
        const names = b.in as unknown[];
        if (!Array.isArray(names) || names.length < 2 || names.length > 3) {
          this.bad(`${path}.in`, "two or three variables");
          break;
        }
        const kinds = new Set(names.map((n) => (isName(n) ? envIn[n] : undefined)));
        names.forEach((n) => this.want(envIn, n, ["text", "verdict"], `${path}.in`));
        if (kinds.size !== 1) this.bad(`${path}.in`, "compare texts with texts, verdicts with verdicts");
        this.pick(envIn, b, path);
        setOut(kinds.has("verdict") ? "verdict" : "compare");
        break;
      }
      case "for_each": {
        this.want(envIn, b.in, ["texts"], `${path}.in`);
        const inner = { ...envIn };
        if (isName(b.as)) inner[b.as] = "text";
        else this.bad(`${path}.as`, `${String(b.as)} is not a variable name`);
        this.stack(b.stack, inner, [...at, index, "stack"], `${path}.stack`);
        break;
      }
      case "file_report":
        this.want(envIn, b.in, ["text"], `${path}.in`);
        if ("title" in b) this.template(envIn, b.title, `${path}.title`);
        break;
      default:
        break;
    }
    return env;
  }
}

/** The problems the hub's validator would name, all of them (it names the first). */
export function analyze(program: Program): Issue[] {
  const checker = new Checker();
  const env: Env = {};
  for (const [name, type] of Object.entries(program.inputs ?? {})) env[name] = type as VarType;
  checker.stack(program.stack, env, [], "stack");
  return checker.issues;
}

/** The variables that exist at a place (before the block at `index` of the stack at `path`). */
export function variablesAt(program: Program, path: StackPath, index: number): Env {
  let found: Env = {};
  const probe: Probe = (at, i, env) => {
    if (i === index && at.length === path.length && at.every((p, k) => p === path[k])) found = env;
  };
  const checker = new Checker(probe);
  const env: Env = {};
  for (const [name, type] of Object.entries(program.inputs ?? {})) env[name] = type as VarType;
  checker.stack(program.stack, env, [], "stack");
  return found;
}

/** The variables of one of the types, at a place (for a typed choice). */
export function namesOfType(env: Env, types: VarType[]): string[] {
  return Object.entries(env)
    .filter(([, t]) => types.includes(t))
    .map(([n]) => n);
}

// --- editing ------------------------------------------------------------------------------------

function freshName(program: Program, base: string): string {
  const used = new Set<string>(Object.keys(program.inputs ?? {}));
  const walk = (blocks: Block[]) => {
    for (const b of blocks) {
      for (const key of ["out", "as"]) if (typeof b[key] === "string") used.add(b[key] as string);
      for (const key of STACK_KEYS) if (Array.isArray(b[key])) walk(b[key] as Block[]);
      if (Array.isArray(b.lanes)) for (const lane of b.lanes as Block[][]) walk(lane);
    }
  };
  walk(program.stack);
  if (!used.has(base)) return base;
  let n = 2;
  while (used.has(`${base}_${n}`)) n += 1;
  return `${base}_${n}`;
}

/** A new block of `kind` with fields that already pass the typing rules at the place, or an
 *  EditError that says what is missing (a loop needs a verdict to test, a comparison two texts). */
export function newBlock(program: Program, kind: string, path: StackPath, index: number): Block {
  if (!KINDS[kind]) throw new EditError(`Unknown block ${kind}.`);
  const env = variablesAt(program, path, index);
  // the newest variable of the type: what the block before it just wrote is the likeliest input
  const first = (types: VarType[]) => namesOfType(env, types).at(-1);
  const text = first(["text"]);
  const cond = (): string => {
    const v = first(["bool", "verdict", "compare"]);
    if (!v) throw new EditError("There is no verdict or yes/no variable to test before this place.");
    const have = env[v];
    return have === "verdict" ? `${v}.ok` : have === "compare" ? `${v}.agree` : v;
  };
  const needText = (): string => {
    if (!text) throw new EditError("There is no text variable before this place.");
    return text;
  };
  const template = () => `{{${needText()}}}`;
  switch (kind) {
    case "ask":
      return { b: "ask", prompt: template(), out: freshName(program, "answer") };
    case "agent":
      return { b: "agent", prompt: template(), out: freshName(program, "result") };
    case "check":
      return { b: "check", in: needText(), out: freshName(program, "verdict") };
    case "revise":
      return { b: "revise", draft: needText(), out: freshName(program, "revised") };
    case "set":
      return { b: "set", value: template(), out: freshName(program, "text") };
    case "if":
      return { b: "if", cond: cond(), then: [], else: [] };
    case "repeat_until":
      return {
        b: "repeat_until",
        cond: cond(),
        max: 3,
        stack: [{ b: "set", value: template(), out: freshName(program, "text") }],
      };
    case "parallel": {
      const a = freshName(program, "lane_a");
      const bName = a === "lane_a" ? "lane_b" : `${a}_b`;
      return {
        b: "parallel",
        max_parallel: 1,
        lanes: [
          [{ b: "set", value: template(), out: a }],
          [{ b: "set", value: template(), out: bName }],
        ],
      };
    }
    case "compare": {
      const texts = namesOfType(env, ["text"]);
      if (texts.length < 2) throw new EditError("A comparison needs two text variables before this place.");
      return { b: "compare", in: texts.slice(-2), out: freshName(program, "compared") };
    }
    case "for_each": {
      const list = first(["texts"]);
      if (!list) throw new EditError("There is no list of texts before this place.");
      return { b: "for_each", in: list, as: freshName(program, "item"), stack: [] };
    }
    case "file_report":
      return { b: "file_report", in: needText() };
    default:
      return { b: kind };
  }
}

/** A copy of the program with `block` inserted. */
export function insertBlock(program: Program, path: StackPath, index: number, block: Block): Program {
  const next = clone(program);
  const stack = mustStackAt(next, path);
  if (index < 0 || index > stack.length) throw new EditError("There is no such place in the stack.");
  stack.splice(index, 0, block);
  return next;
}

export function removeBlock(program: Program, path: StackPath, index: number): Program {
  const next = clone(program);
  const stack = mustStackAt(next, path);
  if (!stack[index]) throw new EditError("There is no such block.");
  stack.splice(index, 1);
  return next;
}

/** Move a block up or down inside its stack (`delta` -1 or 1). */
export function moveWithin(program: Program, path: StackPath, index: number, delta: number): Program {
  const next = clone(program);
  const stack = mustStackAt(next, path);
  const to = index + delta;
  if (!stack[index] || to < 0 || to >= stack.length) throw new EditError("It cannot move further.");
  const [block] = stack.splice(index, 1);
  stack.splice(to, 0, block as Block);
  return next;
}

/** Move a block to another place (`toIndex` counts in the stack as it is before the move). */
export function moveBlock(
  program: Program,
  from: { path: StackPath; index: number },
  to: { path: StackPath; index: number },
): Program {
  const sameStack =
    from.path.length === to.path.length && from.path.every((p, k) => p === to.path[k]);
  // a block cannot go into itself
  const into = [...from.path, from.index];
  if (to.path.length > from.path.length && into.every((p, k) => p === to.path[k])) {
    throw new EditError("A block cannot be moved into itself.");
  }
  const block = clone(stackAt(program, from.path)?.[from.index]);
  if (!block) throw new EditError("There is no such block.");
  // removing the block shifts the indices that follow it in the same stack, and the path of any
  // place below a later sibling of it
  let removed = removeBlock(program, from.path, from.index);
  let target: StackPath = [...to.path];
  let toIndex = to.index;
  if (sameStack && toIndex > from.index) toIndex -= 1;
  const depth = from.path.length;
  if (
    !sameStack &&
    to.path.length > depth &&
    from.path.every((p, k) => p === to.path[k]) &&
    (to.path[depth] as number) > from.index
  ) {
    target = to.path.map((p, k) => (k === depth ? (p as number) - 1 : p));
  }
  removed = insertBlock(removed, target, toIndex, block);
  return removed;
}

/** Put the block into the first stack of the block before it (a loop, a branch). */
export function indent(program: Program, path: StackPath, index: number): Program {
  const stack = stackAt(program, path);
  const previous = index > 0 ? stack?.[index - 1] : undefined;
  if (!previous) throw new EditError("There is no block before it to put it into.");
  const inside = childPaths(previous, path, index - 1)[0];
  if (!inside) throw new EditError("The block before it holds no blocks.");
  const length = stackAt(program, inside)?.length ?? 0;
  return moveBlock(program, { path, index }, { path: inside, index: length });
}

/** Take the block out of its stack and put it right after the block that holds it. */
export function outdent(program: Program, path: StackPath, index: number): Program {
  if (path.length === 0) throw new EditError("It is not inside another block.");
  const lanes = path[path.length - 2] === "lanes";
  const parentLength = lanes ? 3 : 2;
  const parentPath = path.slice(0, path.length - parentLength);
  const parentIndex = path[path.length - parentLength] as number;
  return moveBlock(program, { path, index }, { path: parentPath, index: parentIndex + 1 });
}

/** A copy of the block right after it; the variable it writes gets a free name. */
export function duplicateBlock(program: Program, path: StackPath, index: number): Program {
  const original = stackAt(program, path)?.[index];
  if (!original) throw new EditError("There is no such block.");
  const copy = clone(original);
  if (typeof copy.out === "string") copy.out = freshName(program, copy.out);
  return insertBlock(program, path, index + 1, copy);
}

export function setField(
  program: Program,
  path: StackPath,
  index: number,
  field: string,
  value: unknown,
): Program {
  const next = clone(program);
  const block = mustStackAt(next, path)[index];
  if (!block) throw new EditError("There is no such block.");
  if (value === undefined || value === "" || value === null) delete block[field];
  else block[field] = value;
  return next;
}

/** Edit that is refused when it leaves more problems than there were (`message` names a new one). */
export function checked(before: Program, after: Program): { ok: true } | { ok: false; message: string } {
  const was = analyze(before);
  const now = analyze(after);
  if (now.length <= was.length) return { ok: true };
  const known = new Set(was.map((i) => i.message));
  const fresh = now.find((i) => !known.has(i.message)) ?? now[0];
  return { ok: false, message: fresh ? `${fresh.path}: ${fresh.message}` : "that would break the program" };
}
