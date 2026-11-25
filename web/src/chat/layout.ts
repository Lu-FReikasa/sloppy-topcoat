import type { ChatMessage } from "../types";

/** One answer to a question that needed a check is one block for the reader: the hub's
 *  "looking now" bubble is not shown (the progress line says it while the check runs), and
 *  the report card of the check belongs to the answer: it sits at the end of the answer's own
 *  bubble, folded until the reader opens it. A card whose answer has not come (yet, or at all)
 *  stays a row of its own where it was. */
export interface Arranged {
  message: ChatMessage;
  /** the check's report card, shown inside the answer's bubble */
  report?: ChatMessage;
}

export function arrange(messages: ChatMessage[]): Arranged[] {
  const says = (m: ChatMessage) =>
    m.role === "assistant" && (m.kind === undefined || m.kind === "say");
  const out: Arranged[] = [];
  const held: ChatMessage[] = [];
  messages.forEach((m, i) => {
    if (m.role === "assistant" && m.kind === "report") {
      held.push(m); // until its answer is there
      return;
    }
    if (says(m) && messages[i + 1]?.kind === "report") return;
    out.push({ message: m });
    if (!says(m) || m.status === "running" || !held.length) {
      return;
    }
    // the answer of the check: the card of its task, or (a hub-written closing line carries no
    // task) the one before it
    const at = held.findIndex((c) => c.agent_task_id === m.agent_task_id);
    const [card] = held.splice(at >= 0 ? at : held.length - 1, 1);
    const last = out.at(-1);
    if (card && last) last.report = card;
  });
  // a card whose answer has not come (yet, or at all) stays open where it was
  for (const card of held) {
    const at = out.findIndex((x) => x.message.id > card.id);
    out.splice(at < 0 ? out.length : at, 0, { message: card });
  }
  return out;
}
