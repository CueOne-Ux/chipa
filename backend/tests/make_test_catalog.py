"""
Generate test catalog PDFs that mimic a South African specials leaflet.

Produces two files:
  * catalog_text.pdf  — has a real text layer (best case)
  * catalog_flat.pdf  — flattened to an image, no text layer at all.
                        This forces the OCR path, which is what a real
                        Food Lover's Market / Boxer leaflet looks like.
"""

from __future__ import annotations

import io
from pathlib import Path

import fitz
from PIL import Image, ImageDraw, ImageFont

OUT_DIR = Path(__file__).parent / "fixtures"

LEAFLET = [
    ("HEADER", "FOOD LOVER'S MARKET"),
    ("SUB", "Specials valid 24 July to 30 July 2026"),
    ("ITEM", "Douglasdale Full Cream Milk 2L", "R29.99", "Was R34.99"),
    ("ITEM", "Albany Superior White Bread 700g", "R18.99", None),
    ("ITEM", "Tastic Rice 2kg", "R25.99", "Save R4.00"),
    ("ITEM", "Fresh Chicken Braai Pack 2kg", "R89.99", None),
    ("ITEM", "Eskort Smoked Viennas 500g", "R44.99", "Was R49.99"),
    ("MULTI", "Coca-Cola 2L", "2 for R50"),
    ("ITEM", "Broccoli 350g", "R27.99", None),
    ("ITEM", "Woolworths Fat Free Milk 2L", "R31.99", None),
    ("FOOTER", "E&OE. While stocks last. Selected stores only."),
]


def _font(size: int):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def render_image(width: int = 1240, height: int = 1754) -> Image.Image:
    """Draw the leaflet as a raster image — no text layer survives this."""
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)

    y = 60
    draw.text((60, y), LEAFLET[0][1], fill="black", font=_font(58))
    y += 90
    draw.text((60, y), LEAFLET[1][1], fill="black", font=_font(30))
    y += 80

    for entry in LEAFLET[2:-1]:
        if entry[0] == "ITEM":
            _, name, price, extra = entry
            draw.text((60, y), name, fill="black", font=_font(36))
            y += 48
            draw.text((80, y), price, fill="black", font=_font(46))
            if extra:
                draw.text((320, y + 8), extra, fill="black", font=_font(28))
            y += 78
        elif entry[0] == "MULTI":
            _, name, promo = entry
            draw.text((60, y), name, fill="black", font=_font(36))
            y += 48
            draw.text((80, y), promo, fill="black", font=_font(46))
            y += 78

    draw.text((60, height - 90), LEAFLET[-1][1], fill="black", font=_font(24))
    return image


def build_flat_pdf(path: Path) -> None:
    """Image-only PDF — the realistic case for supermarket leaflets."""
    image = render_image()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4 points
    page.insert_image(fitz.Rect(0, 0, 595, 842), stream=buffer.read())
    doc.save(str(path))
    doc.close()


def build_text_pdf(path: Path) -> None:
    """PDF with a genuine text layer."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)

    y = 60
    page.insert_text((50, y), LEAFLET[0][1], fontsize=24, fontname="helv")
    y += 40
    page.insert_text((50, y), LEAFLET[1][1], fontsize=12, fontname="helv")
    y += 40

    for entry in LEAFLET[2:-1]:
        if entry[0] == "ITEM":
            _, name, price, extra = entry
            page.insert_text((50, y), name, fontsize=13, fontname="helv")
            y += 20
            line = price + (f"   {extra}" if extra else "")
            page.insert_text((70, y), line, fontsize=15, fontname="helv")
            y += 32
        elif entry[0] == "MULTI":
            _, name, promo = entry
            page.insert_text((50, y), name, fontsize=13, fontname="helv")
            y += 20
            page.insert_text((70, y), promo, fontsize=15, fontname="helv")
            y += 32

    page.insert_text((50, 800), LEAFLET[-1][1], fontsize=9, fontname="helv")
    doc.save(str(path))
    doc.close()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    build_text_pdf(OUT_DIR / "catalog_text.pdf")
    build_flat_pdf(OUT_DIR / "catalog_flat.pdf")
    print(f"wrote {OUT_DIR/'catalog_text.pdf'}")
    print(f"wrote {OUT_DIR/'catalog_flat.pdf'}")


if __name__ == "__main__":
    main()
