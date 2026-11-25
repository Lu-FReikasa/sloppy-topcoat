// Number/date formatting helpers. The locale follows the active i18n language.

export function formatInt(n: number, locale: string): string {
  return new Intl.NumberFormat(locale, { maximumFractionDigits: 0 }).format(n);
}

export function formatDecimal(n: number, locale: string, digits = 1): string {
  return new Intl.NumberFormat(locale, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(n);
}

/** Percentage (0..100, rounded) of used/total; null when not computable. */
export function percent(used: number | null, total: number): number | null {
  if (used === null || !Number.isFinite(used) || total <= 0) return null;
  return Math.round((used / total) * 100);
}

export function bytesToGb(bytes: number): number {
  return bytes / 1e9;
}

export function mbToGb(mb: number): number {
  return mb / 1024;
}

export function formatDateTime(iso: string | null, locale: string): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat(locale, { dateStyle: "short", timeStyle: "medium" }).format(d);
}

/** Byte count as "850 MB" or "1,2 GB" (decimal units, like model sizes). */
export function formatBytes(bytes: number, locale: string): string {
  if (bytes < 1e9) return `${formatInt(bytes / 1e6, locale)} MB`;
  return `${formatDecimal(bytesToGb(bytes), locale)} GB`;
}
