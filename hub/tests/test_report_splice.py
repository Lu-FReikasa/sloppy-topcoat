"""A revision changes the model's parts of a report only (2026-11-13), and a report gets a title
that names its content, not the question."""

from topcoat_hub.report_service import clean_title, title_messages
from topcoat_hub.report_splice import splice

ORIGINAL = """---
title: "Frage"
---

# Pixel-Animationen

| Eckdaten | |
|---|---|
| Stand | Entwurf, Version 1 |
| Erstellt | 2026-11-13 13:56:47+00:00 |

## Inhalt

- [Vorwort](#vorwort)
- [1. Alt](#1-alt)
- [Quellenverzeichnis](#quellenverzeichnis)
- [Entstehung](#entstehung)

## Vorwort

Altes Vorwort [1].

### Grenzen dieser Recherche

- Eine Seite war nicht erreichbar.

## 1. Alt

Alter Text.

## Quellenverzeichnis

[1] example.org

## Entstehung

| Lauf | |
|---|---|
| Schritte | 6 |
"""

REVISED = """# Pixel-Animationen

| Eckdaten | |
|---|---|
| Erstellt | 2024-11-13 13:56:47+00:00 |

## Inhalt

- whatever

## Vorwort

Neues Vorwort [1].

### Grenzen dieser Recherche

- erfunden

## 1. Werkzeuge

Neuer Text mit Tabelle.

```mermaid
graph TD
## not a heading
```

## 2. Empfehlung

ComfyUI.

## Quellenverzeichnis

[1] erfunden.example

## Entstehung

| Schritte | 99 |
"""


def test_a_revision_takes_the_foreword_and_chapters_and_keeps_the_codes_parts() -> None:
    out = splice(ORIGINAL, REVISED, "de", 2)
    assert out is not None
    assert "| Stand | Entwurf, Version 2 |" in out
    # the model's parts come from the revision
    assert "Neues Vorwort [1]." in out and "Altes Vorwort" not in out
    assert "## 1. Werkzeuge" in out and "## 2. Empfehlung" in out and "Alter Text" not in out
    assert "## not a heading" in out  # a fence is no section
    # the code's parts stay as they were
    assert "2026-11-13 13:56:47" in out and "2024" not in out
    assert "Eine Seite war nicht erreichbar." in out and "erfunden" not in out
    assert "| Schritte | 6 |" in out and "99" not in out
    # the contents follow the chapters
    assert "- [2. Empfehlung](#2-empfehlung)" in out and "- [Entstehung](#entstehung)" in out
    assert (
        out.index("## Vorwort") < out.index("## 1. Werkzeuge") < out.index("## Quellenverzeichnis")
    )


def test_a_revision_without_foreword_or_chapters_is_not_taken() -> None:
    assert splice(ORIGINAL, "Nur ein Satz ohne Gliederung.", "de") is None


def test_a_title_names_the_content_and_is_no_question() -> None:
    assert clean_title("<think>hm</think>\n**Lokale Pixel-Animation: Werkzeuge im Vergleich**") == (
        "Lokale Pixel-Animation: Werkzeuge im Vergleich"
    )
    assert clean_title("Titel: „Stable Diffusion und AnimateDiff für Pixel-Art“") == (
        "Stable Diffusion und AnimateDiff für Pixel-Art"
    )
    assert clean_title('{\n  "title": "Lokale Werkzeuge für Pixel-Art-Animationen"\n}') == (
        "Lokale Werkzeuge für Pixel-Art-Animationen"
    )
    assert clean_title("Welche Werkzeuge gibt es?") is None
    assert clean_title("kurz") is None
    msgs = title_messages("de", "Welche Werkzeuge?", "Kurz.", ["Werkzeuge", "Empfehlung"])
    assert "Titel" in msgs[0]["content"] and "Werkzeuge; Empfehlung" in msgs[1]["content"]


def test_a_thorough_chapter_waits_for_pages_from_two_sites() -> None:
    from types import SimpleNamespace

    from topcoat_hub.message_tools import _pages_read

    def ev(capability: str, url: str = "") -> SimpleNamespace:
        return SimpleNamespace(source={"capability": capability, "input": {"url": url}})

    searches = [ev("web.search"), ev("web.search")]
    assert _pages_read(searches) == (0, 0)
    one_site = [ev("web.fetch", f"https://a.example.org/{n}") for n in range(3)]
    assert _pages_read(one_site) == (3, 1)
    mixed = [*one_site, ev("web.fetch", "https://b.example.org/x"), ev("knowledge.read")]
    assert _pages_read(mixed) == (5, 3)
