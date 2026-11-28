"""The committed OpenAPI document matches the app (issue #207)."""

from __future__ import annotations

import json

from topcoat_hub.openapi_contract import (
    CONTRACT,
    REGENERATE,
    WEBSOCKETS_KEY,
    build_app,
    main,
    render,
)


def test_committed_openapi_matches_the_app() -> None:
    committed = CONTRACT.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert committed == render(build_app()), (
        f"the hub's API changed: regenerate {CONTRACT.name} with `{REGENERATE}` and commit it"
    )


def test_contract_lists_websockets_beside_the_http_paths() -> None:
    doc = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert "/api/node/ws" in doc[WEBSOCKETS_KEY]
    assert "/api/node/ws" not in doc["paths"]
    assert "get" in doc["paths"]["/api/me"]


def test_check_fails_on_a_stale_file_and_write_fixes_it(tmp_path, capsys) -> None:
    out = tmp_path / "openapi.json"
    assert main(["--check", "--out", str(out)]) == 1  # missing
    out.write_text("{}\n", encoding="utf-8")
    assert main(["--check", "--out", str(out)]) == 1  # stale
    assert REGENERATE in capsys.readouterr().err
    assert main(["--out", str(out)]) == 0
    assert main(["--check", "--out", str(out)]) == 0
    # a CRLF checkout of the same content still counts as current
    out.write_bytes(out.read_bytes().replace(b"\n", b"\r\n"))
    assert main(["--check", "--out", str(out)]) == 0
