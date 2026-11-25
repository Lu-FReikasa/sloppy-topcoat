"""The modules a layout document may name (``proto/ui/modules.json``).

The web build owns the modules (code); the hub only needs to know their ids, whether they need the
admin role and which tool group they need. A test of the web build keeps the list and the web's
registry in step."""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModuleInfo:
    id: str
    kind: str
    admin: bool
    needs: str | None = None

    def view(self) -> dict[str, object]:
        return {"id": self.id, "kind": self.kind, "admin": self.admin, "needs": self.needs}


class Manifest:
    def __init__(self, modules: list[ModuleInfo]) -> None:
        self.modules = {m.id: m for m in modules}

    def get(self, module_id: str) -> ModuleInfo | None:
        return self.modules.get(module_id)

    def __contains__(self, module_id: object) -> bool:
        return module_id in self.modules

    def ids(self) -> list[str]:
        return list(self.modules)


def load_manifest(proto_root: Path) -> Manifest:
    raw = json.loads((proto_root / "ui" / "modules.json").read_text(encoding="utf-8"))
    return Manifest(
        [
            ModuleInfo(
                id=m["id"],
                kind=m.get("kind", "page"),
                admin=bool(m.get("admin", False)),
                needs=m.get("needs"),
            )
            for m in raw["modules"]
        ]
    )
