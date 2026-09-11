"""Turn a design's print file into a digital printable-wall-art product.

Reuses the ``print_file.png`` the artwork engine already produces — no new AI
spend — and packages it as the standard Etsy printable bundle: the artwork fitted
onto the five common print ratios (2:3, 3:4, 4:5, 5:7, ISO A) at 300 DPI, zipped
into one instant-download file. The art is *fitted* (letterboxed on white), never
cropped, so a square/│portrait design is never chopped — it reads as a matted
print. Also builds the digital-listing description with the honest size guidance.

Pure and testable — the orchestration (which products, creating the Etsy digital
listing) lives in :meth:`onassis.content_engine.ContentEngine.make_printables`.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

# The five ratios that cover almost every frame buyers own. Values are just the
# aspect (w:h); ISO A-series is 1:√2 ≈ 1000:1414.
RATIOS: dict[str, tuple[int, int]] = {
    "2x3": (2, 3), "3x4": (3, 4), "4x5": (4, 5), "5x7": (5, 7), "ISO_A": (1000, 1414),
}
# What each ratio prints at (for the buyer-facing description).
RATIO_SIZES = {
    "2x3": "4×6, 8×12, 12×18, 16×24, 20×30 in",
    "3x4": "6×8, 9×12, 12×16, 15×20, 18×24 in",
    "4x5": "8×10, 16×20 in",
    "5x7": "5×7 in",
    "ISO_A": "A5, A4, A3, A2, A1",
}


def build_print_set(src_path: str, out_dir: str, *, dpi: int = 300,
                    max_px: int = 2400, margin: float = 0.05,
                    bg: tuple[int, int, int] = (255, 255, 255)) -> dict[str, Any]:
    """Produce a 300-DPI JPEG per ratio (art fitted on white, never cropped) and
    a single ZIP of them. Returns ``{"files": [...], "zip": path, "ratios": [...]}``.
    """
    from PIL import Image

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    src = Image.open(src_path).convert("RGB")
    long_edge = min(max_px, max(src.size))
    files: list[str] = []
    for label, (rw, rh) in RATIOS.items():
        # Portrait canvas (rw < rh) sized so the long edge == long_edge.
        cw, ch = int(round(long_edge * rw / rh)), long_edge
        canvas = Image.new("RGB", (cw, ch), bg)
        fit = src.copy()
        fit.thumbnail((int(cw * (1 - margin)), int(ch * (1 - margin))),
                      Image.LANCZOS)
        canvas.paste(fit, ((cw - fit.width) // 2, (ch - fit.height) // 2))
        p = out / f"print_{label}.jpg"
        canvas.save(p, "JPEG", quality=92, dpi=(dpi, dpi))
        files.append(str(p))
    zip_path = out / "printable_set.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, arcname=Path(f).name)
    return {"files": files, "zip": str(zip_path), "ratios": list(RATIOS)}


def printable_description(subject: str) -> str:
    """The digital-listing body: what it is, what's included, and the terms —
    written so a buyer immediately understands it's an instant download, not a
    physical item shipped to them."""
    subject = (subject or "wall art").strip()
    ratios = "\n".join(f"• {label.replace('_', ' ')} — prints at {RATIO_SIZES[label]}"
                       for label in RATIOS)
    return (
        f"INSTANT DOWNLOAD — printable {subject}. No physical item is shipped; you "
        "download the files and print them yourself at home, at a local print shop, "
        "or online.\n\n"
        "WHAT YOU GET\nA ZIP containing high-resolution 300 DPI JPEG files in five "
        f"aspect ratios, so it fits standard frames:\n{ratios}\n\n"
        "HOW IT WORKS\n1. Buy and download the ZIP (available instantly after "
        "purchase).\n2. Unzip and choose the ratio that matches your frame.\n3. "
        "Print at home, a local shop, or an online print service.\n\n"
        "PLEASE NOTE\n• Digital product — nothing is posted to you.\n• Colours may "
        "vary slightly between screens and printers.\n• For personal use only; not "
        "for resale or redistribution.\n• Frame not included."
    )
