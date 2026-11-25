"""What the hub loads once for the interface data: the module manifest."""

from dataclasses import dataclass
from pathlib import Path

from topcoat_hub.ui.manifest import Manifest, load_manifest


@dataclass(frozen=True)
class UiRuntime:
    manifest: Manifest


def build_runtime(proto_root: Path) -> UiRuntime:
    return UiRuntime(load_manifest(proto_root))
