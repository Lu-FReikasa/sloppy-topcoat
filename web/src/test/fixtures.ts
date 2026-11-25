import type { NodeDetail, NodeSummary } from "../types";

export function makeNode(overrides: Partial<NodeSummary> = {}): NodeSummary {
  return {
    id: "n1",
    name: "node-nvidia",
    os: "linux",
    arch: "amd64",
    state: "available",
    connected: true,
    last_seen: "2026-01-01T12:00:00Z",
    agent_version: "0.1.0",
    cpu: { model: "Fake CPU", cores: 8 },
    ram_total_mb: 32768,
    ram_used_mb: 8192,
    gpus: [
      {
        index: 0,
        name: "Fake GPU",
        vendor: "nvidia",
        vram_total_mb: 24576,
        vram_used_mb: 8192,
      },
    ],
    runner: { type: "fake", available: true },
    models: [
      { name: "qwen-7b", size_bytes: 4_500_000_000, format: "gguf", loaded: false, vram_used_mb: null },
      { name: "llama-8b", size_bytes: 5_000_000_000, format: "gguf", loaded: true, vram_used_mb: 6000 },
    ],
    loaded: [{ name: "llama-8b", vram_used_mb: 6000, context_length: 8192 }],
    ...overrides,
  };
}

export function makeDetail(overrides: Partial<NodeDetail> = {}): NodeDetail {
  return {
    ...makeNode(),
    events: [
      {
        id: 1,
        kind: "model_loaded",
        data: { model: "llama-8b" },
        ts: "2026-01-01T11:59:00Z",
        received_at: "2026-01-01T11:59:01Z",
      },
    ],
    config: null,
    benchmarks: [],
    ...overrides,
  };
}
