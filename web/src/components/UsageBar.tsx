import { useTranslation } from "react-i18next";
import { formatInt, percent } from "../format";
import type { Gpu } from "../types";

/**
 * "Von anderen Programmen belegt: 6.000 MB · GPU-Last anderer Programme: 95 %", if known.
 * A program in system memory is mentioned once its share reaches `sharedTriggerMb`.
 */
export function ForeignUsage({
  gpu,
  sharedTriggerMb = 1024,
}: {
  gpu: Gpu;
  sharedTriggerMb?: number;
}) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const parts: string[] = [];
  if (gpu.foreign_vram_used_mb !== undefined) {
    parts.push(t("node.foreignVram", { value: formatInt(gpu.foreign_vram_used_mb, locale) }));
  }
  if (gpu.foreign_gpu_util !== undefined) {
    parts.push(t("node.foreignUtil", { value: formatInt(gpu.foreign_gpu_util, locale) }));
  }
  const shared = gpu.foreign_shared_max_mb;
  if (shared !== undefined && shared > 0 && shared >= sharedTriggerMb) {
    parts.push(t("node.foreignShared", { value: formatInt(shared, locale) }));
  }
  if (parts.length === 0) return null;
  return <p className="muted small foreign-usage">{parts.join(" · ")}</p>;
}

interface Props {
  label: string;
  usedMb: number | null;
  totalMb: number;
}

/** VRAM usage bar: used/total MB with percentage; handles unknown usage. */
export function VramBar({ label, usedMb, totalMb }: Props) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const pct = percent(usedMb, totalMb);
  const text =
    pct === null || usedMb === null
      ? t("vram.unknown", { total: formatInt(totalMb, locale) })
      : t("vram.value", {
          used: formatInt(usedMb, locale),
          total: formatInt(totalMb, locale),
          pct,
        });
  const level = pct === null ? "unknown" : pct >= 90 ? "high" : pct >= 70 ? "mid" : "low";
  return (
    <div className="usage">
      <div className="usage-label">
        <span className="usage-name">{label}</span>
        <span className="usage-value">{text}</span>
      </div>
      <div
        className={`usage-track usage-${level}`}
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct ?? undefined}
        aria-valuetext={text}
      >
        <div className="usage-fill" style={{ width: `${Math.min(100, pct ?? 0)}%` }} />
      </div>
    </div>
  );
}
