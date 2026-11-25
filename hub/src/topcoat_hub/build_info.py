"""The build this hub runs: the git commit it was built from, set by the image build as
``TOPCOAT_BUILD`` (``hub/Dockerfile``). ``unknown`` when it was started without it, e.g. a local
``make hub-run``. Shown by ``GET /api/health`` so the running hub can be matched to a commit."""

import os

BUILD = os.environ.get("TOPCOAT_BUILD", "").strip() or "unknown"
