"""A4 raster preparation and the Windows built-in PDF renderer."""

import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .firmware import ROOT

WIDTH, HEIGHT = 9920, 7016  # OpenPrinting foo2xqx-wrapper A4 at 1200x600
MAX_PAGES = 100
SOURCE_RESOLUTIONS = {(300, 300), (600, 600), (1200, 600)}


def _source_resolution(resolution) -> tuple[int, int]:
    resolution = tuple(resolution)
    if resolution not in SOURCE_RESOLUTIONS:
        raise ValueError(f"Unsupported source resolution: {resolution}")
    return resolution


def a4_page(image: Image.Image, *, raster: bool = False,
            resolution: tuple[int, int] = (1200, 600)) -> Image.Image:
    """Keep physical aspect ratio despite the printer's nonsquare pixels.

    PWG pages describe the full sheet at their header's resolution; preserve
    its physical coordinates when converting to the native 1200x600 raster.
    PDF/image pages have square pixels and are fitted at 600 dpi first.
    """
    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        image = Image.alpha_composite(background, rgba).convert("RGB")
    gray = ImageOps.grayscale(image)
    if raster:
        xdpi, ydpi = _source_resolution(resolution)
        source_size = (round(WIDTH * xdpi / 1200), round(HEIGHT * ydpi / 600))
        if abs(gray.width - source_size[0]) > 4 or abs(gray.height - source_size[1]) > 4:
            raise ValueError(f"Expected A4 PWG raster at {xdpi}x{ydpi} near {source_size}, got {gray.size}")
        page = Image.new("L", source_size, 255)
        page.paste(gray, (0, 0))
        # Keep the native path pixel-exact, including its existing small
        # crop/pad allowance for A4 rounding. Resample only other source DPIs.
        return page if source_size == (WIDTH, HEIGHT) else page.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    page = Image.new("L", (WIDTH // 2, HEIGHT), 255)
    gray = ImageOps.contain(gray, page.size, Image.Resampling.LANCZOS)
    page.paste(gray, ((page.width - gray.width) // 2, (page.height - gray.height) // 2))
    return page.resize((WIDTH, HEIGHT), Image.Resampling.BICUBIC)


def write_pbm(image: Image.Image, stream) -> None:
    # Pillow's PBM encoder performs the required inversion (PBM: 1=black).
    image.convert("1", dither=Image.Dither.FLOYDSTEINBERG).save(stream, format="PPM")


def pdf_pages(path: Path, *, resolution: tuple[int, int] = (1200, 600)):
    raster_dpi = min(_source_resolution(resolution))
    if os.name != "nt":
        raise RuntimeError("PDF rendering uses Windows.Data.Pdf on Windows 11")
    (ROOT / "state").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pdf-", dir=ROOT / "state") as output:
        command = [str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
                   "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-File", str(ROOT / "scripts/render-pdf.ps1"), "-InputFile", str(path.resolve()),
                   "-OutputDirectory", output, "-MaxPages", str(MAX_PAGES),
                   "-RasterDpi", str(raster_dpi)]
        result = subprocess.run(command, capture_output=True, timeout=600,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError("Windows PDF renderer failed: " + result.stderr.decode("utf-8", "replace"))
        pages = sorted(Path(output).glob("page-*.png"))
        if not pages:
            raise ValueError("PDF contains no pages")
        for filename in pages:
            with Image.open(filename) as page:
                yield a4_page(page)


def file_pages(path: Path, document_format: str | None = None, *,
               resolution: tuple[int, int] = (1200, 600)):
    resolution = _source_resolution(resolution)
    if document_format == "application/pdf" or path.suffix.lower() == ".pdf":
        yield from pdf_pages(path, resolution=resolution)
    elif document_format == "image/pwg-raster" or path.suffix.lower() in (".pwg", ".ras"):
        from .pwg import iter_pages
        for number, page in enumerate(iter_pages(path.read_bytes()), 1):
            if number > MAX_PAGES:
                raise ValueError("Too many pages")
            # PWG is self-describing: use its actual header DPI, which may
            # differ from the IPP default selected when no job attribute exists.
            yield a4_page(page.image, raster=True, resolution=page.resolution)
    else:
        with Image.open(path) as source:
            count = getattr(source, "n_frames", 1)
            if count > MAX_PAGES:
                raise ValueError("Too many image pages")
            for i in range(count):
                source.seek(i)
                yield a4_page(ImageOps.exif_transpose(source))


def test_page() -> Image.Image:
    page = Image.new("L", (WIDTH // 2, HEIGHT), 255)
    draw = ImageDraw.Draw(page)
    font_path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts/arial.ttf"
    font = ImageFont.truetype(str(font_path), 100) if font_path.exists() else ImageFont.load_default(size=100)
    small = ImageFont.truetype(str(font_path), 64) if font_path.exists() else ImageFont.load_default(size=64)
    draw.rectangle((120, 120, page.width - 121, page.height - 121), outline=0, width=8)
    draw.text((240, 240), "HP LaserJet P1006 / Windows 11 ARM64", font=font, fill=0)
    draw.text((240, 430), "Тест печати: русский текст / English / 0123456789", font=small, fill=0)
    draw.text((240, 600), "A4, simplex, XQX 1200 x 600 dpi", font=small, fill=0)
    for i in range(10):
        x = 240 + i * 440
        draw.rectangle((x, 850, x + 400, 1250), fill=round(i * 255 / 9), outline=0, width=2)
    for width in (1, 2, 4, 8):
        y = 1500 + width * 80
        draw.line((240, y, page.width - 240, y), fill=0, width=width)
    draw.text((240, 2500), "Проверьте: текст, градации серого, линии и поля.", font=small, fill=0)
    # Square measured in physical coordinates: 25.4 mm at 600 dpi.
    draw.rectangle((240, 2900, 839, 3499), outline=0, width=4)
    draw.text((950, 3100), "25.4 x 25.4 mm", font=small, fill=0)
    return page.resize((WIDTH, HEIGHT), Image.Resampling.BICUBIC)
