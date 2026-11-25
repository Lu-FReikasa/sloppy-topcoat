import { useEffect, useState } from "react";



const STEPS = 8;
const TICK_MS = 160;
const ASH = ["░", "▒", "·", " "];

function hash(i: number, step: number): number {
  return (Math.imul(i + 1, 2654435761) ^ Math.imul(step + 1, 40503)) >>> 0;
}

/** One frame of the burning: from the first step on, more and more of the letters of the text turn
 *  into ash (a few kinds of grey, then nothing), the same way for the same step, so a frame can be
 *  tested. Step 0 is the text as it is, the last step is nothing but ash and blanks. */
export function burnFrame(text: string, step: number): string {
  const share = Math.min(step / (STEPS - 1), 1);
  let out = "";
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i] ?? "";
    if (ch === "\n" || ch === " ") {
      out += ch;
      continue;
    }
    const h = hash(i, 0) / 4294967296; // when this letter goes: early or late
    if (h > share) {
      out += ch;
    } else {
      const since = share - h;
      out += ASH[Math.min(Math.floor(since * 6), ASH.length - 1)] ?? " ";
    }
  }
  return out;
}

/** The end of an incognito tab: what was said turns to ash in front of the person's eyes. */
export function Burning({ text }: { text: string }) {
  const [step, setStep] = useState(0);
  useEffect(() => {
    const timer = setInterval(
      () => setStep((s) => Math.min(s + 1, STEPS - 1)),
      TICK_MS,
    );
    return () => clearInterval(timer);
  }, []);
  return (
    <pre className="burning" aria-hidden="true" data-testid="burning">
      {burnFrame(text.slice(0, 1200), step)}
    </pre>
  );
}
