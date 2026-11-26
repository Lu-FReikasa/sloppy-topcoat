"""The GUI counts the hub as lost when its stream stays silent for a while (web/src/api/sse.ts,
LOST_AFTER_MS); a quiet hub must send a keepalive often enough that a healthy stream never looks
lost, even when one keepalive is late."""

import re
from pathlib import Path

from topcoat_hub.config import Settings

SSE_TS = Path(__file__).resolve().parents[2] / "web" / "src" / "api" / "sse.ts"


def test_two_keepalives_fit_into_the_guis_lost_time() -> None:
    match = re.search(r"export const LOST_AFTER_MS = (\d+);", SSE_TS.read_text(encoding="utf-8"))
    assert match, "LOST_AFTER_MS not found in web/src/api/sse.ts"
    lost_after_seconds = int(match.group(1)) / 1000
    keepalive = Settings.model_fields["sse_keepalive_seconds"].default
    assert 2 * keepalive < lost_after_seconds
