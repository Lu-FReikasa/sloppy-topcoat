import { useTranslation } from "react-i18next";
import { formatDecimal, mbToGb } from "../format";
import type { NodeSummary } from "../types";

/** "12,3 / 31,9 GB" style RAM text, or "unbekannt". */
export function useRamText(node: Pick<NodeSummary, "ram_used_mb" | "ram_total_mb">): string {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  if (node.ram_total_mb === null) return t("node.unknown");
  const total = formatDecimal(mbToGb(node.ram_total_mb), locale);
  const used = node.ram_used_mb === null ? "?" : formatDecimal(mbToGb(node.ram_used_mb), locale);
  return `${used} / ${total} GB`;
}

/** "412 GB frei von 954 GB" for the model drive, or null when the node did not report it. */
export function useDiskText(
  node: Pick<NodeSummary, "disk_total_mb" | "disk_free_mb">,
): string | null {
  const { t, i18n } = useTranslation();
  if (node.disk_total_mb == null || node.disk_free_mb == null) return null;
  const locale = i18n.language;
  return t("node.diskValue", {
    free: formatDecimal(mbToGb(node.disk_free_mb), locale),
    total: formatDecimal(mbToGb(node.disk_total_mb), locale),
  });
}

/** True when less than 10 % (or under 20 GB) of the model drive is free. */
export function diskLow(node: Pick<NodeSummary, "disk_total_mb" | "disk_free_mb">): boolean {
  if (node.disk_total_mb == null || node.disk_free_mb == null || node.disk_total_mb <= 0) {
    return false;
  }
  return node.disk_free_mb < 20 * 1024 || node.disk_free_mb / node.disk_total_mb < 0.1;
}
