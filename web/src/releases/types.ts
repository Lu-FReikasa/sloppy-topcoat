// Administration → Releases (NS-01, ARCHITECTURE D80): what the hub sends about builds.

export type BuildState =
  | "requested"
  | "building"
  | "built"
  | "failed"
  | "published";
export type BuildKind = "node_release" | "hub_image";
export type BuildSource = "release_repo" | "dev_mirror";

export interface BuildArtifact {
  kind?: "binary" | "image";
  name: string;
  os?: string;
  arch?: string;
  size: number;
  sha256?: string;
  digest?: string;
}

export interface ReleaseBuild {
  id: string;
  kind: BuildKind;
  source: BuildSource;
  ref: string;
  commit: string | null;
  version: string | null;
  state: BuildState;
  node: string | null;
  changes: string;
  artifacts: BuildArtifact[];
  smoke: { ok: boolean; summary: string } | null;
  rollout: RolloutAttempt[];
  log: string;
  error: string | null;
  channel: string | null;
  key_id: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  published_at: string | null;
}

export interface RolloutAttempt {
  at: string;
  node: string;
  dry_run: boolean;
  ok: boolean;
  output: string;
  error?: string;
}

export interface ReleasesOverview {
  rollout_node?: string | null;
  enabled: boolean;
  channel_enabled: boolean;
  key_ids: string[];
  build_nodes: { id: string; name: string; state: string }[];
  builds: ReleaseBuild[];
  published: {
    channel: string;
    os: string;
    arch: string;
    version: string;
    size: number;
    sha256: string;
    created: string;
  }[];
}

export interface ReleaseManifest {
  platform: string;
  name: string;
  size: number;
  sha256: string;
  manifest: string;
}

export interface ReleasePreview {
  channel: string;
  created: string;
  previous_version: string | null;
  key_ids: string[];
  build: ReleaseBuild;
  manifests: ReleaseManifest[];
}

export interface BuildStart {
  kind: BuildKind;
  ref: string;
  version?: string | null;
  source: BuildSource;
}
