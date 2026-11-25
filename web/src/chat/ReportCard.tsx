import { useRef, useState } from "react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import type {
  ReportCard as Card,
  ReportEvidence,
  ReportFailure,
} from "../types";
import { StoredCheck } from "./CheckPanel";
import { copyToClipboard } from "./Markdown";
import { IssueCard } from "./IssueCard";
import { IterateOffer } from "./IterateOffer";
import { PreparedChanges } from "./PreparedChanges";
import { TaskProposals } from "./TaskProposals";
import { PushFromChat } from "./PushFromChat";

/** The report card of a server check (E3-07 D3). The hub built it from the check's diagnosis and
 *  evidence — nothing on it was written by the model. Every evidence mark ([live E1]) is a button
 *  that opens the excerpt it names; assumptions stand apart from the findings; a proposal carries
 *  its impact and its way back (or says the check did not give them). */

/** Why a check stopped early, in words: the code of the hub when the catalogue does not know it. */
function stopWords(t: TFunction, reason: string): string {
  return t(`report.stop.${reason}`, { defaultValue: reason });
}

/** The table of a check whose order asked for one: the columns of its first row, every value as
 *  text; null when the check wrote none. */
export function tableOf(card: Card): { columns: string[]; rows: string[][] } | null {
  const rows = (card.table ?? []).filter(
    (r) => r && typeof r === "object" && Object.keys(r).length > 0,
  );
  const first = rows[0];
  if (!first) return null;
  const columns = Object.keys(first);
  const text = (v: unknown) => (v === null || v === undefined ? "" : String(v));
  return { columns, rows: rows.map((r) => columns.map((c) => text(r[c]))) };
}

/** The card as plain text in the user's language: what the copy button puts on the clipboard. */
export function reportToText(t: TFunction, card: Card): string {
  const lines: string[] = [];
  if (card.incomplete) {
    lines.push(
      `${t("report.incomplete")}${card.incomplete_reason ? `: ${stopWords(t, card.incomplete_reason)}` : ""}`,
    );
  }
  if (card.summary) lines.push(`${t("report.summary")}: ${card.summary}`);
  if (card.summary && card.summary_has_assumptions)
    lines.push(t("report.summaryAssumed"));
  if (card.findings.length > 0) {
    lines.push("", `${t("report.findings")}:`);
    for (const f of card.findings) {
      const marks = f.evidence.length > 0 ? ` [${f.evidence.join(", ")}]` : "";
      lines.push(`- [${f.tag}] ${f.claim}${marks}`);
    }
  }
  const table = tableOf(card);
  if (table) {
    lines.push("", `${t("report.table")}:`);
    lines.push(table.columns.join(" | "));
    for (const row of table.rows) lines.push(row.join(" | "));
  }
  if (card.assumptions.length > 0) {
    lines.push(
      "",
      `${t("report.assumptions")} (${t("report.assumptionsHint")}):`,
    );
    for (const a of card.assumptions) {
      lines.push(
        `- ${a.claim}${a.confirm_with ? ` (${t("report.confirmWith")}: ${a.confirm_with})` : ""}`,
      );
    }
  }
  if (card.proposal) {
    lines.push("", `${t("report.proposal")}: ${card.proposal.text}`);
    if (card.proposal.impact)
      lines.push(`- ${t("report.impact")}: ${card.proposal.impact}`);
    if (card.proposal.rollback)
      lines.push(`- ${t("report.rollback")}: ${card.proposal.rollback}`);
  }
  for (const f of card.failures ?? []) {
    lines.push(`${t("report.failed")}: ${failureText(f)}`);
  }
  if (card.coverage && card.coverage.not_asked.length > 0) {
    lines.push("", coverageText(t, card.coverage));
  }
  if (card.evidence.length > 0) {
    lines.push("", `${t("report.evidence")}:`);
    for (const e of card.evidence) {
      lines.push(`${e.ref} — ${evidenceTitle(e)}`);
      if (e.excerpt) lines.push(e.excerpt.trimEnd());
    }
  }
  return lines.join("\n");
}

function coverageText(
  t: TFunction,
  c: { asked: string[]; not_asked: string[] },
): string {
  return t("report.coverage", {
    asked: c.asked.join(", ") || "–",
    notAsked: c.not_asked.join(", "),
  });
}

function failureText(f: ReportFailure): string {
  const what = f.target || `${f.capability ?? "?"} @ ${f.node ?? "?"}`;
  return `${what}: ${f.message || f.code || "?"}`;
}

function evidenceTitle(e: ReportEvidence): string {
  return [e.capability, e.node ? `@ ${e.node}` : "", e.url ?? ""]
    .filter(Boolean)
    .join(" ");
}

function Marks({
  refs,
  tag,
  onOpen,
}: {
  refs: string[];
  tag: string;
  onOpen: (ref: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <>
      {refs.map((ref) => (
        <button
          key={ref}
          type="button"
          className={`evidence-mark badge badge-tag-${tag}`}
          aria-label={t("report.showEvidence", { ref })}
          onClick={() => onOpen(ref)}
        >
          {tag} {ref}
        </button>
      ))}
    </>
  );
}

export function ReportCardView({
  card,
  api,
  folded = false,
  withChanges = true,
}: {
  card: Card;
  api?: ApiClient;
  folded?: boolean;
  /** false when the caller shows the prepared changes itself (the talk page does) */
  withChanges?: boolean;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(!folded);
  const [copied, setCopied] = useState(false);
  const [opened, setOpened] = useState<Record<string, boolean>>({});
  const table = tableOf(card);
  // the evidence list is folded: it is for checking, not for reading
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const refs = useRef<Record<string, HTMLDetailsElement | null>>({});

  function showEvidence(ref: string) {
    setEvidenceOpen(true);
    setOpened((o) => ({ ...o, [ref]: true }));
    const el = refs.current[ref];
    // after the excerpt is open, bring it into view (jsdom has no scrollIntoView)
    window.setTimeout(
      () => el?.scrollIntoView?.({ block: "nearest", behavior: "smooth" }),
      0,
    );
  }

  return (
    <section
      className={`report-card${card.incomplete ? " incomplete" : ""}`}
      data-testid="report-card"
      aria-label={t("report.title")}
    >
      <header className="report-head">
        <strong>{t("report.title")}</strong>
        <span className="spacer" />
        <button
          type="button"
          onClick={() =>
            void copyToClipboard(reportToText(t, card)).then(setCopied)
          }
          data-testid="report-copy"
        >
          {copied ? t("chat.copied") : t("report.copy")}
        </button>
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          data-testid="report-toggle"
        >
          {open ? t("report.collapse") : t("report.expand")}
        </button>
      </header>
      {card.incomplete && (
        <p className="notice-line" data-testid="report-incomplete">
          {t("report.incomplete")}
          {card.incomplete_reason
            ? `: ${stopWords(t, card.incomplete_reason)}`
            : ""}
        </p>
      )}
      {card.summary && <p className="report-summary">{card.summary}</p>}
      {card.summary && card.summary_has_assumptions && (
        <p className="muted small" data-testid="report-summary-assumed">
          {t("report.summaryAssumed")}
        </p>
      )}
      {(card.failures ?? []).map((f, i) => (
        <p key={i} className="notice-line" data-testid="report-failure">
          <strong>{t("report.failed")}:</strong> {failureText(f)}
        </p>
      ))}
      {open && (
        <div className="report-body" data-testid="report-body">
          {card.findings.length > 0 && (
            <>
              <h4>{t("report.findings")}</h4>
              <ul className="report-findings">
                {card.findings.map((f, i) => (
                  <li key={i}>
                    {f.claim}{" "}
                    <Marks
                      refs={f.evidence}
                      tag={f.tag}
                      onOpen={showEvidence}
                    />
                    {f.quote && (
                      <q className="report-quote muted small" data-testid="report-quote">
                        {f.quote}
                      </q>
                    )}
                    {f.unquoted && (
                      <span className="muted small"> {t("report.unquoted")}</span>
                    )}
                    {f.contradicts && f.contradicts.length > 0 && (
                      <span className="muted small">
                        {" "}
                        {t("report.contradicts", {
                          refs: f.contradicts.join(", "),
                        })}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </>
          )}
          {table && (
            <section className="report-table-wrap" data-testid="report-table">
              <h4>{t("report.table")}</h4>
              <table className="report-table">
                <thead>
                  <tr>
                    {table.columns.map((c) => (
                      <th key={c} scope="col">
                        {c}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {table.rows.map((row, i) => (
                    <tr key={i}>
                      {row.map((v, j) => (
                        <td key={j}>{v}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}
          {card.assumptions.length > 0 && (
            <section
              className="report-assumptions"
              data-testid="report-assumptions"
            >
              <h4>
                {t("report.assumptions")}{" "}
                <span className="muted small">
                  {t("report.assumptionsHint")}
                </span>
              </h4>
              <ul>
                {card.assumptions.map((a, i) => (
                  <li key={i}>
                    {a.claim}
                    {a.confirm_with && (
                      <span className="muted small">
                        {" "}
                        ({t("report.confirmWith")}: {a.confirm_with})
                      </span>
                    )}
                    {a.misquoted && (
                      <span className="muted small" data-testid="report-misquoted">
                        {" "}
                        {t("report.misquoted", { quote: a.misquoted })}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}
          {card.proposal && (
            <section className="report-proposal" data-testid="report-proposal">
              <h4>{t("report.proposal")}</h4>
              <p>{card.proposal.text}</p>
              {(card.proposal.impact || card.proposal.rollback) && (
                <dl>
                  {card.proposal.impact && (
                    <>
                      <dt>{t("report.impact")}</dt>
                      <dd>{card.proposal.impact}</dd>
                    </>
                  )}
                  {card.proposal.rollback && (
                    <>
                      <dt>{t("report.rollback")}</dt>
                      <dd>{card.proposal.rollback}</dd>
                    </>
                  )}
                </dl>
              )}
            </section>
          )}
          {card.coverage && card.coverage.not_asked.length > 0 && (
            <p className="notice-line" data-testid="report-coverage">
              {coverageText(t, card.coverage)}
            </p>
          )}
          {card.notes.map((n, i) => (
            <p key={i} className="muted small">
              {n}
            </p>
          ))}
          {card.evidence.length > 0 && (
            <details
              className="report-evidence-all"
              open={evidenceOpen}
              onToggle={(ev) =>
                setEvidenceOpen((ev.currentTarget as HTMLDetailsElement).open)
              }
            >
              <summary>
                <h4>
                  {t("report.evidence")} ({card.evidence.length})
                </h4>
              </summary>
              <ul className="report-evidence">
                {card.evidence.map((e) => (
                  <li key={e.ref}>
                    <details
                      id={`evidence-${e.ref}`}
                      ref={(el) => {
                        refs.current[e.ref] = el;
                      }}
                      open={opened[e.ref] ?? false}
                      onToggle={(ev) => {
                        // read it now: currentTarget is gone when the state updater runs
                        const isOpen = (ev.currentTarget as HTMLDetailsElement)
                          .open;
                        setOpened((o) => ({ ...o, [e.ref]: isOpen }));
                      }}
                    >
                      <summary>
                        <span className={`badge badge-tag-${e.tag}`}>
                          {e.ref}
                        </span>{" "}
                        <code>{evidenceTitle(e)}</code>
                      </summary>
                      <pre className="report-excerpt">
                        {e.excerpt || t("report.noExcerpt")}
                      </pre>
                      {e.truncated && (
                        <p className="muted small">{t("report.truncated")}</p>
                      )}
                    </details>
                  </li>
                ))}
              </ul>
            </details>
          )}
          {card.task_id && api && (
            <div className="report-steps">
              <StoredCheck api={api} taskId={card.task_id} />
            </div>
          )}
        </div>
      )}
      {api && <PushFromChat api={api} evidence={card.evidence} />}
      {api && withChanges && card.changes_for && (
        <PreparedChanges api={api} taskId={card.changes_for} />
      )}
      {api && card.iterate && <IterateOffer api={api} offer={card.iterate} />}
      {api && card.issue && <IssueCard api={api} issue={card.issue} />}
      {api && card.proposals_for && (
        <TaskProposals api={api} taskId={card.proposals_for} />
      )}
    </section>
  );
}
