import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import type { MachinesView } from "../types";
import { Dialog } from "./Dialog";

/** Which node runs on which machine, and which machine is a virtual machine on which host. The
 *  names the nodes carry already point at their machine (`tools-alpha` is on `alpha`); this is for
 *  what the names cannot say, such as a VM and its host. */
export function MachinesEditor({
  api,
  view,
  onClose,
  onSaved,
}: {
  api: ApiClient;
  view: MachinesView;
  onClose: () => void;
  onSaved: (v: MachinesView) => void;
}) {
  const { t } = useTranslation();
  const [machines, setMachines] = useState(
    view.machines.map((m) => ({
      key: m.key,
      name: m.name,
      kind: m.kind,
      host: m.host ?? "",
    })),
  );
  const [assign, setAssign] = useState<Record<string, string>>({
    ...view.node_machine,
  });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const physical = machines.filter((m) => m.kind === "physical");

  function setMachine(key: string, patch: Partial<(typeof machines)[number]>) {
    setMachines((list) =>
      list.map((m) => (m.key === key ? { ...m, ...patch } : m)),
    );
  }

  async function save() {
    setBusy(true);
    setError(null);
    try {
      // only what differs from the machine the node's name points at is stored as an assignment
      const wanted: Record<string, string> = {};
      for (const [node, key] of Object.entries(assign))
        if (key !== view.node_machine[node] || key in (view.assign ?? {}))
          wanted[node] = key;
      const saved = await api.putMachines({
        machines: machines.map((m) => ({
          key: m.key,
          name: m.name.trim() || m.key,
          kind: m.kind,
          ...(m.kind === "vm" ? { host: m.host } : {}),
        })),
        assign: wanted,
      });
      onSaved(saved);
      onClose();
    } catch {
      setError(t("machines.saveFailed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog title={t("machines.title")} onClose={onClose}>
      <p className="muted small">{t("machines.hint")}</p>
      <h3>{t("machines.machines")}</h3>
      <ul className="machine-edit-list" data-testid="machines-list">
        {machines.map((m) => (
          <li key={m.key}>
            <input
              aria-label={t("machines.name", { key: m.key })}
              value={m.name}
              onChange={(e) => setMachine(m.key, { name: e.target.value })}
            />
            <select
              aria-label={t("machines.kind", { key: m.key })}
              value={m.kind}
              onChange={(e) =>
                setMachine(m.key, { kind: e.target.value as "physical" | "vm" })
              }
            >
              <option value="physical">{t("machines.physical")}</option>
              <option value="vm">{t("machines.vm")}</option>
            </select>
            {m.kind === "vm" && (
              <select
                aria-label={t("machines.host", { key: m.key })}
                value={m.host}
                onChange={(e) => setMachine(m.key, { host: e.target.value })}
              >
                <option value="">{t("machines.hostPick")}</option>
                {physical
                  .filter((p) => p.key !== m.key)
                  .map((p) => (
                    <option key={p.key} value={p.key}>
                      {p.name}
                    </option>
                  ))}
              </select>
            )}
          </li>
        ))}
      </ul>
      <h3>{t("machines.nodes")}</h3>
      <ul className="machine-edit-list" data-testid="machines-nodes">
        {Object.keys(assign)
          .sort()
          .map((node) => (
            <li key={node}>
              <span>{node}</span>
              <select
                aria-label={t("machines.nodeOn", { node })}
                value={assign[node]}
                onChange={(e) =>
                  setAssign((a) => ({ ...a, [node]: e.target.value }))
                }
              >
                {machines.map((m) => (
                  <option key={m.key} value={m.key}>
                    {m.name}
                  </option>
                ))}
              </select>
            </li>
          ))}
      </ul>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="dialog-actions">
        <button
          type="button"
          className="primary"
          disabled={busy}
          onClick={() => void save()}
        >
          {t("machines.save")}
        </button>
        <button type="button" onClick={onClose}>
          {t("machines.cancel")}
        </button>
      </div>
    </Dialog>
  );
}
