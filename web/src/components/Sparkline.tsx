/** A small line chart of the last samples: no axes, the current value is written next to it. */
export function Sparkline({
  values,
  max,
  label,
  width = 120,
  height = 28,
}: {
  /** Samples oldest first; null = unknown at that moment (a gap). */
  values: (number | null)[];
  /** Value at the top of the chart (100 for percentages). */
  max: number;
  /** Spoken description, e.g. "GPU load, now 42 %". */
  label: string;
  width?: number;
  height?: number;
}) {
  const known = values.filter((v): v is number => v !== null);
  if (known.length < 2) return <span className="muted small">–</span>;
  const step = width / Math.max(values.length - 1, 1);
  const y = (v: number) => height - 1 - (Math.min(Math.max(v, 0), max) / max) * (height - 2);
  // consecutive known samples form one stroke; a null starts a new one
  const strokes: string[] = [];
  let cur = "";
  values.forEach((v, i) => {
    if (v === null) {
      if (cur) strokes.push(cur);
      cur = "";
      return;
    }
    cur += `${cur ? "L" : "M"}${(i * step).toFixed(1)} ${y(v).toFixed(1)}`;
  });
  if (cur) strokes.push(cur);
  return (
    <svg
      className="sparkline"
      role="img"
      aria-label={label}
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      preserveAspectRatio="none"
    >
      <line x1="0" x2={width} y1={height - 0.5} y2={height - 0.5} className="sparkline-base" />
      {strokes.map((d) => (
        <path key={d.slice(0, 24)} d={d} className="sparkline-line" fill="none" />
      ))}
    </svg>
  );
}
