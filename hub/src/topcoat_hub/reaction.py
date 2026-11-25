"""A reaction instead of an answer: a message that only acknowledges (thanks, ok, understood) needs
no text block. The hub decides by rule, never a model: a small model must not "react away" a
question. Only a short message that holds nothing but such a word qualifies, the assistant's last
message must not be a question the user may be answering ("ok" to "Soll ich das tun?"), and the
choice of emoji is small and fixed."""

import re

THUMBS = "👍"
THANKS = "🙏"
DONE = "🎉"

_END = r"[\s.!,]*(?:👍|🙏|🙂|😊|:\))?[\s.!]*"
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        THANKS,
        re.compile(
            r"(?i)(?:(?:vielen|besten|tausend|herzlichen)\s+dank|danke(?:\s+(?:dir|schön|sehr|dir\s+"
            r"vielmals))?|dank\s+dir|thanks?(?:\s+(?:a\s+lot|so\s+much))?|thank\s+you(?:\s+(?:very\s+"
            r"much|so\s+much))?|thx|ty|merci)" + _END + r"$"
        ),
    ),
    (
        DONE,
        re.compile(
            r"(?i)(?:geschafft|(?:es\s+)?(?:läuft|klappt|funktioniert)(?:\s+(?:jetzt|wieder|"
            r"alles))*|passt\s+jetzt|(?:it\s+)?works(?:\s+now)?|solved|gelöst)" + _END + r"$"
        ),
    ),
    (
        THUMBS,
        re.compile(
            r"(?i)(?:ok(?:ay)?(?:\s+(?:danke|super|gut|passt|alles\s+klar))?|alles\s+klar|passt|"
            r"super|perfekt|prima|gut|top|verstanden|alles\s+gut|gut\s+zu\s+wissen|"
            r"got\s+it|sounds\s+good|great|perfect|nice|cool|fine|understood|"
            r"einverstanden|klingt\s+gut|mach\s+ich|machen\s+wir)" + _END + r"$"
        ),
    ),
)
MAX_CHARS = 40


def emoji_for(text: str, last_assistant: str = "") -> str | None:
    """The emoji to answer ``text`` with, or ``None`` when it needs a real answer."""
    text = text.strip()
    if not text or len(text) > MAX_CHARS or "?" in text or "\n" in text:
        return None
    if last_assistant.rstrip().endswith("?"):
        return None  # the user may be answering a question of the assistant
    for emoji, pattern in _PATTERNS:
        if pattern.match(text):
            return emoji
    return None
