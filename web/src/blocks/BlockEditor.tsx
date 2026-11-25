import { Fragment, useState, type DragEvent, type KeyboardEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import {
  EditError,
  KINDS,
  childPaths,
  duplicateBlock,
  fieldTypes,
  indent,
  insertBlock,
  moveBlock,
  moveWithin,
  namesOfType,
  newBlock,
  outdent,
  removeBlock,
  setField,
  stackAt,
  variablesAt,
  type Block,
  type Program,
  type StackPath,
} from "./edit";

/** What the hub's catalogue tells the editor (`GET /api/blocks/catalogue`). */
export interface Catalogue {
  ranks: string[];
  suites: string[];
  compare_modes: string[];
  hook_booleans: string[];
  hook_numbers: Record<string, [number, number]>;
  applications: string[];
  depths: string[];
}

interface Selection {
  path: StackPath;
  index: number;
}

export interface EditorProps {
  program: Program;
  catalogue: Catalogue | null;
  /** a change of structure (add, move, remove ...): the page checks it and keeps or refuses it;
   *  it says which (the selection follows a change only when it was kept) */
  onStructure: (next: Program) => boolean;
  /** a change inside a block (a field): kept as typed, the hub's check says whether it is valid */
  onFields: (next: Program) => void;
  /** something the editor itself refuses to do */
  onRefuse: (message: string) => void;
}

const TEMPLATES = ["prompt", "system", "value", "title", "instruction", "reason"];
const NUMBERS = ["max", "max_tokens", "temperature", "max_parallel"];
const HIDDEN = new Set(["then", "else", "stack", "lanes", "guard", "pick", "hooks"]);
const DRAG = "application/x-topcoat-block";

const same = (a: StackPath, b: StackPath) => a.length === b.length && a.every((p, i) => p === b[i]);

function stackLabel(path: StackPath): string {
  const last = path[path.length - 1];
  if (path[path.length - 2] === "lanes") return `lane ${(last as number) + 1}`;
  return last === "stack" ? "do" : String(last);
}

export function BlockEditor({ program, catalogue, onStructure, onFields, onRefuse }: EditorProps) {
  const { t } = useTranslation();
  const [sel, setSel] = useState<Selection | null>(null);

  const selected = sel ? stackAt(program, sel.path)?.[sel.index] : undefined;

  function run(action: () => Program, then?: () => void) {
    try {
      if (onStructure(action())) then?.();
    } catch (err) {
      if (err instanceof EditError) onRefuse(err.message);
      else throw err;
    }
  }

  const add = (kind: string, at?: { path: StackPath; index: number }) => {
    const place = at ?? (sel ? { path: sel.path, index: sel.index + 1 } : { path: [], index: program.stack.length });
    run(
      () => insertBlock(program, place.path, place.index, newBlock(program, kind, place.path, place.index)),
      () => setSel({ path: place.path, index: place.index }),
    );
  };

  // The actions take the block they act on (the keys name it; the buttons use the selection).
  const move = (delta: number, at: Selection | null = sel) =>
    at && run(() => moveWithin(program, at.path, at.index, delta), () => setSel({ ...at, index: at.index + delta }));
  const nest = (at: Selection | null = sel) =>
    at &&
    run(
      () => indent(program, at.path, at.index),
      () => {
        const previous = stackAt(program, at.path)?.[at.index - 1];
        const inside = previous ? childPaths(previous, at.path, at.index - 1)[0] : undefined;
        if (inside) setSel({ path: inside, index: stackAt(program, inside)?.length ?? 0 });
      },
    );
  const unnest = (at: Selection | null = sel) =>
    at &&
    run(
      () => outdent(program, at.path, at.index),
      () => {
        const lanes = at.path[at.path.length - 2] === "lanes";
        const cut = lanes ? 3 : 2;
        setSel({
          path: at.path.slice(0, at.path.length - cut),
          index: (at.path[at.path.length - cut] as number) + 1,
        });
      },
    );
  const copy = (at: Selection | null = sel) =>
    at && run(() => duplicateBlock(program, at.path, at.index), () => setSel({ ...at, index: at.index + 1 }));
  const remove = (at: Selection | null = sel) =>
    at && run(() => removeBlock(program, at.path, at.index), () => setSel(null));

  function key(e: KeyboardEvent, at: Selection) {
    if (e.target !== e.currentTarget) return; // keys typed in a field are the field's
    const act = (fn: () => void) => {
      e.preventDefault();
      fn();
    };
    if (e.altKey && e.key === "ArrowUp") act(() => move(-1, at));
    else if (e.altKey && e.key === "ArrowDown") act(() => move(1, at));
    else if (e.altKey && e.key === "ArrowRight") act(() => nest(at));
    else if (e.altKey && e.key === "ArrowLeft") act(() => unnest(at));
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "d") act(() => copy(at));
    else if (e.key === "Delete") act(() => remove(at));
  }

  function drop(e: DragEvent, to: { path: StackPath; index: number }) {
    e.preventDefault();
    const raw = e.dataTransfer.getData(DRAG) || e.dataTransfer.getData("text/plain");
    if (raw.startsWith("kind:")) return add(raw.slice(5), to);
    if (raw.startsWith("move:")) {
      const from = JSON.parse(raw.slice(5)) as Selection;
      run(
        () => moveBlock(program, from, to),
        () => setSel(null),
      );
    }
  }

  // (plain functions, not components: a component defined here would be made anew on every
  // change and the focused block would lose its focus)
  function slot(path: StackPath, index: number) {
    return (
      <li
        key={`slot-${index}`}
        className="block-slot"
        data-testid="block-slot"
        aria-hidden="true"
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => drop(e, { path, index })}
      />
    );
  }

  function stack(path: StackPath) {
    const blocks = stackAt(program, path) ?? [];
    return (
      <ol className="block-tree">
        {slot(path, 0)}
        {blocks.map((block, i) => {
          const here = { path, index: i };
          const on = sel !== null && same(sel.path, path) && sel.index === i;
          return (
            <Fragment key={i}>
              <li
                data-testid={`block-${block.b}`}
                tabIndex={0}
                draggable
                aria-selected={on}
                className={on ? "block-selected" : undefined}
                onFocus={(e) => e.target === e.currentTarget && setSel(here)}
                onClick={(e) => {
                  e.stopPropagation();
                  setSel(here);
                }}
                onKeyDown={(e) => key(e, here)}
                onDragStart={(e) => {
                  e.stopPropagation();
                  e.dataTransfer.setData(DRAG, `move:${JSON.stringify(here)}`);
                  e.dataTransfer.setData("text/plain", `move:${JSON.stringify(here)}`);
                }}
              >
                <span className="block-kind">{block.b}</span>{" "}
                <span className="muted small">{summaryOf(block)}</span>
                {childPaths(block, path, i).map((child) => (
                  <div key={child.join("/")} className="block-child">
                    <span className="muted small">{stackLabel(child)}</span>
                    {stack(child)}
                  </div>
                ))}
              </li>
              {slot(path, i + 1)}
            </Fragment>
          );
        })}
      </ol>
    );
  }

  return (
    <div className="block-editor" data-testid="block-editor">
      <div role="toolbar" aria-label={t("blocksPage.palette")} className="actions">
        {Object.keys(KINDS).map((kind) => (
          <button
            key={kind}
            type="button"
            draggable
            onClick={() => add(kind)}
            onDragStart={(e) => {
              e.dataTransfer.setData(DRAG, `kind:${kind}`);
              e.dataTransfer.setData("text/plain", `kind:${kind}`);
            }}
            title={t("blocksPage.addHint")}
            aria-label={t("blocksPage.add", { kind })}
          >
            + {kind}
          </button>
        ))}
      </div>
      <div role="toolbar" aria-label={t("blocksPage.selected")} className="actions">
        <button type="button" disabled={!sel} onClick={() => move(-1)}>
          {t("blocksPage.moveUp")}
        </button>
        <button type="button" disabled={!sel} onClick={() => move(1)}>
          {t("blocksPage.moveDown")}
        </button>
        <button type="button" disabled={!sel} onClick={() => nest()}>
          {t("blocksPage.indent")}
        </button>
        <button type="button" disabled={!sel} onClick={() => unnest()}>
          {t("blocksPage.outdent")}
        </button>
        <button type="button" disabled={!sel} onClick={() => copy()}>
          {t("blocksPage.duplicate")}
        </button>
        <button type="button" disabled={!sel} onClick={() => remove()}>
          {t("blocksPage.remove")}
        </button>
      </div>
      <p className="muted small">{t("blocksPage.keys")}</p>
      {stack([])}
      {sel && selected ? (
        <FieldEditor
          key={`${sel.path.join("/")}:${sel.index}`}
          program={program}
          sel={sel}
          block={selected}
          catalogue={catalogue}
          onChange={onFields}
        />
      ) : (
        <p className="muted small">{t("blocksPage.noSelection")}</p>
      )}
    </div>
  );
}

function summaryOf(block: Block): string {
  const bits: string[] = [];
  for (const field of ["prompt", "value"]) {
    if (typeof block[field] === "string") bits.push(`"${(block[field] as string).slice(0, 50)}"`);
  }
  for (const field of ["in", "draft", "cond"]) {
    const value = block[field];
    if (typeof value === "string") bits.push(value);
    else if (Array.isArray(value)) bits.push((value as string[]).join(" + "));
  }
  if (typeof block.out === "string") bits.push(`→ ${block.out}`);
  return bits.join(" · ");
}

// --- the fields of the selected block ---------------------------------------------------------

function FieldEditor({
  program,
  sel,
  block,
  catalogue,
  onChange,
}: {
  program: Program;
  sel: Selection;
  block: Block;
  catalogue: Catalogue | null;
  onChange: (next: Program) => void;
}) {
  const { t } = useTranslation();
  const spec = KINDS[block.b];
  const env = variablesAt(program, sel.path, sel.index);
  const set = (field: string, value: unknown) => onChange(setField(program, sel.path, sel.index, field, value));
  const label = (field: string) => t(`blocksPage.field.${field}`, { defaultValue: field });
  const fields = [...(spec?.required ?? []), ...(spec?.optional ?? [])].filter((f) => !HIDDEN.has(f));

  const condChoices = Object.entries(env).flatMap(([name, type]) => {
    if (type === "bool") return [name, `not ${name}`];
    if (type === "verdict") return [`${name}.ok`, `not ${name}.ok`];
    if (type === "compare") return [`${name}.agree`, `not ${name}.agree`];
    return [];
  });

  const select = (field: string, options: string[], value: unknown, optional: boolean) => {
    const current = typeof value === "string" ? value : "";
    const all = current && !options.includes(current) ? [...options, current] : options;
    return (
      <select value={current} onChange={(e) => set(field, e.target.value)} aria-label={label(field)}>
        {(optional || !current) && <option value="">{optional ? t("blocksPage.unset") : "–"}</option>}
        {all.map((o) => (
          <option key={o} value={o}>
            {o}
            {!options.includes(o) ? " (!)" : ""}
          </option>
        ))}
      </select>
    );
  };

  return (
    <fieldset className="block-fields" data-testid="block-fields">
      <legend>{t("blocksPage.block", { kind: block.b })}</legend>
      {fields.map((field) => {
        const optional = !(spec?.required ?? []).includes(field);
        const value = block[field];
        const types = fieldTypes(block.b, field);
        let control: ReactNode;
        if (block.b === "compare" && field === "in") {
          const candidates = namesOfType(env, ["text", "verdict"]);
          const chosen = Array.isArray(value) ? (value as string[]) : [];
          control = (
            <span>
              {candidates.map((name) => (
                <label key={name}>
                  <input
                    type="checkbox"
                    checked={chosen.includes(name)}
                    onChange={(e) =>
                      set(field, e.target.checked ? [...chosen, name] : chosen.filter((n) => n !== name))
                    }
                  />{" "}
                  {name}{" "}
                </label>
              ))}
            </span>
          );
        } else if (types) {
          control = select(field, namesOfType(env, types), value, optional);
        } else if (field === "cond") {
          control = select(field, condChoices, value, false);
        } else if (field === "app") {
          control = select(field, catalogue?.applications ?? [], value, true);
        } else if (field === "depth") {
          control = select(field, catalogue?.depths ?? [], value, true);
        } else if (field === "mode") {
          control = select(field, catalogue?.compare_modes ?? [], value, true);
        } else if (field === "rule") {
          control = select(field, ["all", "majority"], value, true);
        } else if (TEMPLATES.includes(field)) {
          control = (
            <textarea
              rows={2}
              value={typeof value === "string" ? value : ""}
              onChange={(e) => set(field, e.target.value)}
              aria-label={label(field)}
            />
          );
        } else if (NUMBERS.includes(field)) {
          control = (
            <input
              type="number"
              step={field === "temperature" ? "0.1" : "1"}
              value={typeof value === "number" ? value : ""}
              onChange={(e) => set(field, e.target.value === "" ? undefined : Number(e.target.value))}
              aria-label={label(field)}
            />
          );
        } else {
          control = (
            <input
              value={typeof value === "string" ? value : ""}
              onChange={(e) => set(field, e.target.value)}
              aria-label={label(field)}
            />
          );
        }
        return (
          <div key={field} className="form-grid">
            <span>{label(field)}</span>
            {control}
          </div>
        );
      })}
      {spec?.optional.includes("pick") && (
        <PickEditor
          value={block.pick as Record<string, unknown> | undefined}
          env={env}
          catalogue={catalogue}
          onChange={(v) => set("pick", v)}
        />
      )}
      {block.b === "agent" && (
        <HooksEditor
          value={block.hooks as Record<string, unknown> | undefined}
          catalogue={catalogue}
          onChange={(v) => set("hooks", v)}
        />
      )}
    </fieldset>
  );
}

function PickEditor({
  value,
  env,
  catalogue,
  onChange,
}: {
  value: Record<string, unknown> | undefined;
  env: Record<string, string>;
  catalogue: Catalogue | null;
  onChange: (next: Record<string, unknown> | undefined) => void;
}) {
  const { t } = useTranslation();
  const pick = value ?? {};
  const update = (key: string, v: unknown) => {
    const next = { ...pick };
    if (v === undefined || v === "" || (Array.isArray(v) && v.length === 0) || v === false) delete next[key];
    else next[key] = v;
    onChange(Object.keys(next).length > 0 ? next : undefined);
  };
  const others = namesOfType(env as never, ["text", "verdict", "compare"]);
  const avoided = (pick.other_than as string[] | undefined) ?? [];
  return (
    <fieldset className="block-pick">
      <legend>{t("blocksPage.pick")}</legend>
      <label className="form-grid">
        <span>{t("blocksPage.field.rank")}</span>
        <select
          value={typeof pick.rank === "string" ? pick.rank : ""}
          onChange={(e) => update("rank", e.target.value)}
          aria-label={t("blocksPage.field.rank")}
        >
          <option value="">{t("blocksPage.unset")}</option>
          {(catalogue?.ranks ?? []).map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
      </label>
      {pick.rank === "rated" && (
        <label className="form-grid">
          <span>{t("blocksPage.field.suite")}</span>
          <select
            value={typeof pick.suite === "string" ? pick.suite : ""}
            onChange={(e) => update("suite", e.target.value)}
            aria-label={t("blocksPage.field.suite")}
          >
            <option value="">{t("blocksPage.unset")}</option>
            {(catalogue?.suites ?? []).map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
      )}
      <label className="form-grid">
        <span>{t("blocksPage.field.fixed")}</span>
        <input
          value={typeof pick.fixed === "string" ? pick.fixed : ""}
          onChange={(e) => update("fixed", e.target.value)}
          aria-label={t("blocksPage.field.fixed")}
        />
      </label>
      <div>
        <span>{t("blocksPage.field.other_than")}</span>{" "}
        {others.map((name) => (
          <label key={name}>
            <input
              type="checkbox"
              checked={avoided.includes(name)}
              onChange={(e) =>
                update("other_than", e.target.checked ? [...avoided, name] : avoided.filter((n) => n !== name))
              }
            />{" "}
            {name}{" "}
          </label>
        ))}
      </div>
      <label>
        <input
          type="checkbox"
          checked={pick.exclusive === true}
          onChange={(e) => update("exclusive", e.target.checked)}
        />{" "}
        {t("blocksPage.field.exclusive")}
      </label>
    </fieldset>
  );
}

function HooksEditor({
  value,
  catalogue,
  onChange,
}: {
  value: Record<string, unknown> | undefined;
  catalogue: Catalogue | null;
  onChange: (next: Record<string, unknown> | undefined) => void;
}) {
  const { t } = useTranslation();
  const hooks = value ?? {};
  const update = (key: string, v: unknown) => {
    const next = { ...hooks };
    if (v === undefined) delete next[key];
    else next[key] = v;
    onChange(Object.keys(next).length > 0 ? next : undefined);
  };
  return (
    <fieldset className="block-hooks">
      <legend>{t("blocksPage.hooks")}</legend>
      {(catalogue?.hook_booleans ?? []).map((name) => (
        <label key={name} className="form-grid">
          <span>{t(`blocksPage.field.${name}`, { defaultValue: name })}</span>
          <select
            value={hooks[name] === undefined ? "" : String(hooks[name])}
            onChange={(e) => update(name, e.target.value === "" ? undefined : e.target.value === "true")}
            aria-label={t(`blocksPage.field.${name}`, { defaultValue: name })}
          >
            <option value="">{t("blocksPage.hookFrame")}</option>
            <option value="true">{t("blocksPage.hookOn")}</option>
            <option value="false">{t("blocksPage.hookOff")}</option>
          </select>
        </label>
      ))}
      {Object.entries(catalogue?.hook_numbers ?? {}).map(([name, [low, high]]) => (
        <label key={name} className="form-grid">
          <span>{t(`blocksPage.field.${name}`, { defaultValue: name })}</span>
          <input
            type="number"
            min={low}
            max={high}
            value={typeof hooks[name] === "number" ? (hooks[name] as number) : ""}
            placeholder={t("blocksPage.hookFrame")}
            onChange={(e) => update(name, e.target.value === "" ? undefined : Number(e.target.value))}
            aria-label={t(`blocksPage.field.${name}`, { defaultValue: name })}
          />
        </label>
      ))}
    </fieldset>
  );
}
