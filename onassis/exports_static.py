"""Serve the generated ``exports/`` artwork over HTTPS from the app itself.

While ONASSIS is proving the business model we avoid the infrastructure of an
object store: the FastAPI app serves the print files directly, so Gelato can
fetch ``{GELATO_FILE_BASE_URL}/<campaign>/<product_key>/print_file.png`` where
``GELATO_FILE_BASE_URL`` is simply ``https://<server>/exports``.

This is deliberately **replaceable**: the Gelato connector only ever builds a URL
from ``file_base_url`` (it never uploads), so swapping to an object store later
is just a config change — point ``GELATO_FILE_BASE_URL`` at the bucket and drop
this mount. Nothing in the connector changes.

**Security posture** (production-ready for this stage):

* Only image assets are served (``.png/.jpg/.jpeg/.webp/.svg/.gif``). The internal
  JSON manifests (``listing.json``, ``manifest.json``) that sit alongside the
  images are **never** exposed — a request for them returns 404.
* No directory listing (Starlette ``StaticFiles`` with ``html=False``).
* Path traversal is blocked by ``StaticFiles`` (every resolved path is confined
  to the exports root).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from starlette.responses import PlainTextResponse, Response
from starlette.staticfiles import StaticFiles

from onassis.config import ROOT_DIR
from onassis.logger import get_logger

log = get_logger(__name__)

# The only extensions ever served — the sellable artwork/print files and the
# short-form video clips (Sprint 48), which must be fetchable to be posted.
# Everything else under exports/ (listing.json, manifest.json, the reel sidecar
# json with captions, …) stays private.
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", ".mp4", ".webm"}

MOUNT_PATH = "/exports"


def exports_dir(config: Any) -> Path:
    """Resolve the exports directory the same way the Listing Factory writes it."""
    base = Path((getattr(config, "listing", None) or {}).get("exports_dir", "exports"))
    if not base.is_absolute():
        base = ROOT_DIR / base
    return base


class ImageOnlyStaticFiles(StaticFiles):
    """A ``StaticFiles`` that serves ONLY image assets; anything else is 404.

    This keeps the public surface to exactly what an external fulfiller (Gelato)
    needs — the images — and never the internal commercial metadata written
    alongside them."""

    async def get_response(self, path: str, scope) -> Response:
        # `path` is the request path relative to the mount, already normalised by
        # StaticFiles (traversal is blocked upstream). We simply refuse anything
        # that is not an allowed image type.
        if os.path.splitext(path)[1].lower() not in ALLOWED_EXTENSIONS:
            return PlainTextResponse("Not found", status_code=404)
        return await super().get_response(path, scope)


def mount_exports(app: Any, config: Any) -> Path:
    """Mount the exports directory at ``/exports`` (image files only).

    Returns the served directory. Safe to call once at app construction: the
    directory is created if missing so a fresh deployment mounts cleanly.
    """
    directory = exports_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    app.mount(MOUNT_PATH,
              ImageOnlyStaticFiles(directory=str(directory), html=False),
              name="exports")
    log.info("Serving generated artwork at %s (image files only) from %s",
             MOUNT_PATH, directory)
    return directory
