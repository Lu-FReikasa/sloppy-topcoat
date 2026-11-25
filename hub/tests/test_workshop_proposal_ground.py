"""A file change of the workshop as a proposal (D99): grounded in a read, shown as a diff.
The checks of ``ProposalService._check`` on fakes, no database."""

import uuid
from types import SimpleNamespace
from typing import Any

from topcoat_hub.proposals import ProposalService, workshop_preview
from topcoat_hub.tools import Caller

NODE_ID = str(uuid.uuid4())
SCHEMAS = {
    "repo.edit": {
        "type": "object",
        "required": ["repo", "path", "old", "new"],
        "properties": {k: {"type": "string"} for k in ("repo", "path", "old", "new")},
    },
    "repo.write": {
        "type": "object",
        "required": ["repo", "path", "content"],
        "properties": {k: {"type": "string"} for k in ("repo", "path", "content")},
    },
    "repo.apply_patch": {
        "type": "object",
        "required": ["repo", "patch"],
        "properties": {k: {"type": "string"} for k in ("repo", "patch")},
    },
}
PROPOSABLE = [
    {"capability": c, "node": "dev-node", "node_id": NODE_ID, "input_schema": s}
    for c, s in SCHEMAS.items()
]
FIELDS = {
    "reason": "the title is wrong",
    "effect": "changes one line in the scratch copy",
    "who_notices": "nobody, the copy is not pushed",
    "way_back": "repo.reset",
}


def service() -> ProposalService:
    settings = SimpleNamespace(proposal_ttl_seconds=600, proposals_enabled=True)
    return ProposalService(None, None, settings, lambda *a: None)  # type: ignore[arg-type]


def ev(capability: str, **inp: Any) -> SimpleNamespace:
    return SimpleNamespace(
        source={"node": "dev-node", "capability": capability, "input": inp}, excerpt="..."
    )


def check(cap: str, tool_input: dict[str, Any], **evidence: SimpleNamespace):
    item = {
        "node": "dev-node",
        "capability": cap,
        "input": tool_input,
        "evidence": list(evidence),
        **FIELDS,
    }
    tags = {ref: "live" for ref in evidence}
    caller = Caller(user_id=uuid.uuid4(), admin=True, via="gui")
    return service()._check(caller, uuid.uuid4(), item, PROPOSABLE, dict(evidence), tags)


EDIT = {"repo": "app", "path": "src/a.py", "old": "x = 1", "new": "x = 2"}


def test_an_edit_after_a_read_of_that_file_is_accepted_with_a_diff() -> None:
    p, why = check("repo.edit", EDIT, E1=ev("repo.read", repo="app", path="src/a.py"))
    assert p is not None, why
    assert p.preview.splitlines() == ["--- src/a.py", "+++ src/a.py", "-x = 1", "+x = 2"]
    assert p.content_hash  # the diff is part of what the hash covers


def test_the_path_is_compared_in_its_plain_form() -> None:
    p, why = check("repo.edit", EDIT, E1=ev("repo.read", repo="app", path="./src/a.py"))
    assert p is not None, why


def test_an_edit_of_a_file_nobody_read_is_dropped_with_the_reason() -> None:
    p, why = check("repo.edit", EDIT, E1=ev("repo.read", repo="app", path="src/b.py"))
    assert p is None and "no cited result is a read of src/a.py" in why


def test_a_read_of_another_repository_does_not_ground_it() -> None:
    p, why = check("repo.edit", EDIT, E1=ev("repo.read", repo="other", path="src/a.py"))
    assert p is None and "read of src/a.py" in why


def test_a_new_file_needs_a_look_at_the_repository() -> None:
    new = {"repo": "app", "path": "docs/new.md", "content": "a\nb"}
    p, why = check("repo.write", new, E1=ev("repo.list", repo="app", path=""))
    assert p is not None, why
    assert p.preview.splitlines()[0].startswith("+++ docs/new.md") and "+a" in p.preview
    nothing, why = check("repo.write", new, E1=ev("docker.ps"))
    assert nothing is None and "read of docs/new.md" in why


def test_a_patch_needs_a_look_at_its_repository() -> None:
    patch = {"repo": "app", "patch": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"}
    p, why = check("repo.apply_patch", patch, E1=ev("repo.diff", repo="app"))
    assert p is not None and p.preview == patch["patch"], why
    assert check("repo.apply_patch", patch, E1=ev("repo.diff", repo="x"))[0] is None


def test_a_proposal_needs_live_evidence_as_before() -> None:
    item = {"node": "dev-node", "capability": "repo.edit", "input": EDIT, "evidence": [], **FIELDS}
    caller = Caller(user_id=uuid.uuid4(), admin=True, via="gui")
    p, why = service()._check(caller, uuid.uuid4(), item, PROPOSABLE, {}, {})
    assert p is None and "no live evidence" in why


def test_a_long_write_is_shown_cut_with_the_number_of_lines_left() -> None:
    text = workshop_preview("repo.write", {"path": "f", "content": "\n".join(["l"] * 200)})
    assert "200 lines" in text and text.endswith("... 120 more lines")


def test_an_edit_that_cites_no_evidence_takes_the_read_the_record_shows() -> None:
    read = ev("repo.read", repo="app", path="src/a.py")
    item = {"node": "dev-node", "capability": "repo.edit", "input": EDIT, "evidence": [], **FIELDS}
    caller = Caller(user_id=uuid.uuid4(), admin=True, via="gui")
    p, why = service()._check(caller, uuid.uuid4(), item, PROPOSABLE, {"E1": read}, {"E1": "live"})
    assert p is not None, why
    assert p.evidence == ["E1"]  # the hub named it, not the model


def test_without_a_read_of_that_file_in_the_record_there_is_still_no_proposal() -> None:
    other = ev("repo.read", repo="app", path="src/b.py")
    item = {"node": "dev-node", "capability": "repo.edit", "input": EDIT, "evidence": [], **FIELDS}
    caller = Caller(user_id=uuid.uuid4(), admin=True, via="gui")
    p, why = service()._check(caller, uuid.uuid4(), item, PROPOSABLE, {"E1": other}, {"E1": "live"})
    assert p is None and "read of src/a.py" in why
    # a restart is not a file change: it still has to cite its own evidence
    restart = {
        **item,
        "capability": "repo.write",
        "input": {"repo": "app", "path": "x", "content": "y"},
    }
    nobody, _ = service()._check(caller, uuid.uuid4(), restart, PROPOSABLE, {}, {})
    assert nobody is None
