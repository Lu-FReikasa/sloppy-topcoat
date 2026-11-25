import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import type { MemoryTag, MemoryTagTree } from "../types";
import { askConfirm } from "../components/askConfirm";

/** The name a tag is shown with: the fixed top level is translated, every other tag is its own word. */
export function tagLabel(
  tag: Pick<MemoryTag, "name" | "root">,
  t: (k: string, o?: Record<string, unknown>) => string,
): string {
  if (tag.root) return t(`memory.tagTree.root.${tag.name}`, { defaultValue: tag.name });
  if (SEEDED_TAGS.has(tag.name)) return t(`memory.tagTree.seeded.${tag.name}`, { defaultValue: tag.name });
  return tag.name;
}

/** The tags the hub seeds below "person" (memory_tags.SEEDED): built in, so they are translated too. */
const SEEDED_TAGS = new Set(["health", "finance", "private"]);

/** Who decides about a topic: the person or a program. */
export function responsibleLabel(
  key: string,
  t: (k: string, o?: Record<string, unknown>) => string,
): string {
  return key === "person"
    ? t("memory.tagTree.person")
    : t(`inbox.apps.${key}`, { defaultValue: key });
}

/** The person's tag tree (ARCHITECTURE D74): what notes are about, who decides when two programs
 *  remember a topic differently, and which branches are sensitive. The top level is fixed; below it
 *  the person adds, renames, moves, merges and removes tags (notes are never lost: they go to the
 *  tag that takes over). Tags an assistant made are marked until the person has looked at them. */
export function TagTree({ api }: { api: ApiClient }) {
  const { t, i18n } = useTranslation();
  const [tree, setTree] = useState<MemoryTagTree | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fail = useCallback(
    (err: unknown) => {
      if (!(err instanceof UnauthorizedError))
        setError(describeError(toActionError(err), t, i18n));
    },
    [t, i18n],
  );

  useEffect(() => {
    api.memoryTags().then(setTree, fail);
  }, [api, fail]);

  async function change(fn: () => Promise<MemoryTagTree>) {
    setError(null);
    try {
      setTree(await fn());
    } catch (err) {
      fail(err);
    }
  }

  if (!tree)
    return error ? (
      <p className="error" role="alert">
        {error}
      </p>
    ) : (
      <p className="muted">{t("app.loading")}</p>
    );
  const children = new Map<string | null, MemoryTag[]>();
  for (const tag of tree.tags) {
    const list = children.get(tag.parent_id) ?? [];
    list.push(tag);
    children.set(tag.parent_id, list);
  }
  const unreviewed = tree.tags.filter((x) => !x.reviewed).length;

  const branch = (parent: string | null, depth: number) => (
    <ul className={depth === 1 ? "tag-tree" : "tag-branch"}>
      {(children.get(parent) ?? [])
        .slice()
        .sort((a, b) => (a.root || b.root ? 0 : a.name.localeCompare(b.name)))
        .map((tag) => (
          <li key={tag.id} data-testid="tag-node">
            <TagRow
              tag={tag}
              tree={tree}
              depth={depth}
              editing={open === tag.id}
              onToggle={() => setOpen(open === tag.id ? null : tag.id)}
              change={change}
              api={api}
            />
            {children.has(tag.id) && branch(tag.id, depth + 1)}
          </li>
        ))}
    </ul>
  );

  return (
    <section
      className="tag-tree-section"
      aria-label={t("memory.tagTree.title")}
    >
      <h2>{t("memory.tagTree.title")}</h2>
      <p className="muted small">{t("memory.tagTree.hint")}</p>
      {unreviewed > 0 && (
        <p className="notice" role="status" data-testid="tags-unreviewed">
          {t("memory.tagTree.unreviewed", { count: unreviewed })}
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {branch(null, 1)}
    </section>
  );
}

interface RowProps {
  tag: MemoryTag;
  tree: MemoryTagTree;
  depth: number;
  editing: boolean;
  onToggle: () => void;
  change: (fn: () => Promise<MemoryTagTree>) => Promise<void>;
  api: ApiClient;
}

function TagRow({
  tag,
  tree,
  depth,
  editing,
  onToggle,
  change,
  api,
}: RowProps) {
  const { t } = useTranslation();
  const [name, setName] = useState(tag.name);
  const [description, setDescription] = useState(tag.description);
  const [child, setChild] = useState("");
  const [into, setInto] = useState("");
  const eff = tag.effective;
  const others = tree.tags.filter((x) => x.id !== tag.id);

  function addChild(e: FormEvent) {
    e.preventDefault();
    const next = child.trim();
    if (!next) return;
    setChild("");
    void change(() => api.createMemoryTag({ name: next, parent_id: tag.id }));
  }

  return (
    <div className="tag-row">
      <span className="tag-head">
        <strong>{tagLabel(tag, t)}</strong>
        {(tag.root || tag.description) && (
          <span className="muted small">
            {" "}
            —{" "}
            {tag.root
              ? t(`memory.tagTree.rootHint.${tag.name}`, {
                  defaultValue: tag.description,
                })
              : SEEDED_TAGS.has(tag.name)
                ? t(`memory.tagTree.seededHint.${tag.name}`, { defaultValue: tag.description })
                : tag.description}
          </span>
        )}
        <span className="muted small" data-testid="tag-notes">
          {" "}
          · {t("memory.tagTree.notes", { count: tag.notes })}
        </span>
        <span
          className={tag.responsible ? "small" : "muted small"}
          data-testid="tag-responsible"
        >
          {" "}
          ·{" "}
          {t("memory.tagTree.decides", {
            who: responsibleLabel(eff.responsible, t),
          })}
        </span>
        {eff.sensitive && " "}
        {eff.sensitive && (
          <span className="badge badge-sensitive" data-testid="tag-sensitive">
            {t("memory.tagTree.sensitive")}
            {eff.released.length > 0 &&
              ` · ${t("memory.tagTree.releasedFor", { list: eff.released.map((p) => responsibleLabel(p, t)).join(", ") })}`}
          </span>
        )}
        {!tag.reviewed && (
          <>
            {" "}
            <span className="badge" data-testid="tag-new">
              {t("memory.tagTree.new")}
            </span>{" "}
            <button
              type="button"
              className="linklike"
              onClick={() =>
                void change(() =>
                  api.updateMemoryTag(tag.id, { reviewed: true }),
                )
              }
            >
              {t("memory.tagTree.keep")}
            </button>
          </>
        )}{" "}
        <button
          type="button"
          className="linklike"
          aria-expanded={editing}
          onClick={onToggle}
        >
          {editing ? t("memory.tagTree.close") : t("memory.tagTree.edit")}
        </button>
      </span>
      {editing && (
        <div className="tag-edit" data-testid="tag-edit">
          {!tag.root && (
            <label>
              {t("memory.tagTree.name")}{" "}
              <input
                value={name}
                maxLength={30}
                onChange={(e) => setName(e.target.value)}
              />
            </label>
          )}
          <label>
            {t("memory.tagTree.description")}{" "}
            <input
              value={description}
              maxLength={200}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          <button
            type="button"
            onClick={() =>
              void change(() =>
                api.updateMemoryTag(
                  tag.id,
                  tag.root ? { description } : { name, description },
                ),
              )
            }
          >
            {t("memory.tagTree.save")}
          </button>
          <label>
            {t("memory.tagTree.responsible")}{" "}
            <select
              value={tag.responsible ?? ""}
              onChange={(e) =>
                void change(() =>
                  api.updateMemoryTag(tag.id, {
                    responsible: e.target.value || null,
                  }),
                )
              }
            >
              {!tag.root && (
                <option value="">{t("memory.tagTree.inherit")}</option>
              )}
              {tree.responsible.map((r) => (
                <option key={r} value={r}>
                  {responsibleLabel(r, t)}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t("memory.tagTree.sensitiveLabel")}{" "}
            <select
              value={tag.sensitive === null ? "" : tag.sensitive ? "yes" : "no"}
              onChange={(e) =>
                void change(() =>
                  api.updateMemoryTag(tag.id, {
                    sensitive:
                      e.target.value === "" ? null : e.target.value === "yes",
                  }),
                )
              }
            >
              {!tag.root && (
                <option value="">{t("memory.tagTree.inherit")}</option>
              )}
              <option value="yes">{t("memory.tagTree.yes")}</option>
              <option value="no">{t("memory.tagTree.no")}</option>
            </select>
          </label>
          {!tag.root && (
            <>
              <label>
                {t("memory.tagTree.mergeInto")}{" "}
                <select value={into} onChange={(e) => setInto(e.target.value)}>
                  <option value="">–</option>
                  {others.map((x) => (
                    <option key={x.id} value={x.id}>
                      {tagLabel(x, t)}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                disabled={!into}
                onClick={() =>
                  void change(() => api.mergeMemoryTag(tag.id, into))
                }
              >
                {t("memory.tagTree.merge")}
              </button>
              <button
                type="button"
                className="danger"
                onClick={async () => {
                  if (
                    await askConfirm(
                      t("memory.tagTree.confirmDelete", { tag: tag.name }),
                    )
                  )
                    void change(() => api.deleteMemoryTag(tag.id));
                }}
              >
                {t("memory.tagTree.delete")}
              </button>
            </>
          )}
          {depth < tree.max_depth && (
            <form onSubmit={addChild} className="tag-add">
              <input
                type="text"
                aria-label={t("memory.tagTree.addChild", {
                  tag: tagLabel(tag, t),
                })}
                placeholder={t("memory.tagTree.addChild", {
                  tag: tagLabel(tag, t),
                })}
                value={child}
                maxLength={30}
                onChange={(e) => setChild(e.target.value)}
              />
            </form>
          )}
        </div>
      )}
    </div>
  );
}
