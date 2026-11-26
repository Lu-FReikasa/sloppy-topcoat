"""An employee's result in two forms, ``summary`` for the person and ``brief`` for the secretary
(result_forms.py, issue #35, D112): a private detail never reaches the secretary's text."""

import json

import pytest

from topcoat_hub import messages, result_forms

HOSTS = ["node-amd", "nas-cellar"]
USERS = ["alice"]
DOMAINS = ["example.lan"]
SECRET = "sk-" + "A1b2C3d4" * 6  # an API key the secret filter knows


def research(summary: str, **extra: object) -> dict[str, object]:
    return {
        "summary": summary,
        "findings": [
            {
                "claim": "The cache holds 10 GB.",
                "quote": "the cache on node-amd at 10.0.0.12 holds 10 GB",
                "source_url": "https://docs.example.org/cache",
            }
        ],
        **extra,
    }


def dev(summary: str, **extra: object) -> dict[str, object]:
    return {
        "summary": summary,
        "branch": "fix/slow-start",
        "tests": {"state": "passed", "summary": "12 passed"},
        "acceptance": [
            {"item": "starts in 2 s", "met": True},
            {"item": "no warning", "met": False},
        ],
        "findings": [{"claim": "the pool was too small", "path": "hub/src/pool.py"}],
        **extra,
    }


def visible(form: result_forms.ForSecretary) -> str:
    return json.dumps(form.as_dict(), ensure_ascii=False)


def test_the_brief_is_written_by_code_from_the_payload() -> None:
    brief = result_forms.brief_of(
        "research.result",
        research("Local image models need 8 GB. Details follow.", open_points=["price"]),
    )
    assert brief.startswith("research result: Local image models need 8 GB.")
    assert "Details follow" not in brief  # the first sentence only
    assert "1 finding(s)" in brief and "1 open point(s)" in brief
    brief = result_forms.brief_of("dev.result", dev("Fixed the slow start."))
    assert "on branch fix/slow-start" in brief and "tests passed" in brief
    assert "acceptance 1/2 met" in brief
    assert len(result_forms.brief_of("admin.result", {"summary": "x" * 5000})) <= 600


def test_a_result_is_filed_with_its_brief_and_an_own_brief_is_kept() -> None:
    payload = research("All good.")
    filed = result_forms.with_brief("research.result", payload)
    assert filed["brief"] == result_forms.brief_of("research.result", payload)
    assert "brief" not in payload  # the caller's dict is left alone
    own = result_forms.with_brief("admin.result", {"summary": "ok", "brief": "  three hosts up  "})
    assert own["brief"] == "three hosts up"
    assert result_forms.with_brief("project.note", {"statement": "x"}) == {"statement": "x"}
    for type_, body in (("research.result", filed), ("dev.result", dev("ok", brief="short"))):
        messages.check_payload(type_, body)  # the registry takes the brief


def test_a_clean_result_reaches_the_secretary_in_both_forms() -> None:
    form = result_forms.for_secretary(
        "dev.result", dev("Fixed the slow start."), HOSTS, USERS, DOMAINS
    )
    assert form.summary == "Fixed the slow start."
    assert form.brief.startswith("dev result: Fixed the slow start.")
    assert form.refs == [
        {"kind": "file", "ref": "hub/src/pool.py"},
        {"kind": "branch", "ref": "fix/slow-start"},
    ]
    assert form.withheld == []


@pytest.mark.parametrize(
    ("detail", "kind"),
    [
        ("node-amd", "host"),
        ("10.0.0.12", "ip"),
        ("alice", "user"),
        ("ops@example.lan", "email"),
        ("aa:bb:cc:dd:ee:ff", "mac"),
    ],
)
def test_a_private_detail_never_reaches_the_secretary(detail: str, kind: str) -> None:
    summary = f"The backup on {detail} failed last night."
    for type_, payload in (
        ("research.result", research(summary)),
        ("dev.result", dev(summary)),
        ("admin.result", {"summary": summary}),
    ):
        form = result_forms.for_secretary(type_, payload, HOSTS, USERS, DOMAINS)
        text = visible(form)
        assert detail not in text, (type_, text)
        assert kind in form.withheld
        assert form.summary == result_forms.pointer(type_)
        assert form.brief == result_forms.pointer(type_)  # the brief repeats the first sentence


def test_a_secret_never_reaches_the_secretary_and_the_pointer_names_the_application() -> None:
    form = result_forms.for_secretary(
        "admin.result", {"summary": f"The key is {SECRET}.", "brief": "a key was found"}, HOSTS
    )
    assert SECRET not in visible(form)
    assert form.summary == (
        "The result holds private details; they stay in Administration. Open it there."
    )
    assert form.brief == "a key was found"  # a clean brief still goes
    de = result_forms.for_secretary(
        "admin.result", {"summary": f"Der Schlüssel ist {SECRET}."}, HOSTS, language="de"
    )
    assert de.summary.startswith("Das Ergebnis enthält private Angaben; sie bleiben in Verwaltung")


def test_files_go_by_reference_and_never_with_their_content_or_quotes() -> None:
    payload = dev("Fixed it.")
    payload["findings"].append({"claim": "config", "path": "/srv/node-amd/secrets.yml"})
    form = result_forms.for_secretary("dev.result", payload, HOSTS, USERS, DOMAINS)
    text = visible(form)
    assert "node-amd" not in text and "host" in form.withheld  # a path naming a host is left out
    assert {"kind": "file", "ref": "hub/src/pool.py"} in form.refs
    # the findings (claims, quotes) never enter the secretary's text
    form = result_forms.for_secretary("research.result", research("Cache size."), HOSTS)
    assert "10.0.0.12" not in visible(form) and "holds 10 GB" not in visible(form)


def test_only_results_of_employees_have_two_forms() -> None:
    with pytest.raises(ValueError):
        result_forms.for_secretary("project.note", {"statement": "x"})
