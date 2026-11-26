"""Pixel art from any image, and sprite sheets (ARCHITECTURE D126). Pure functions over Pillow
images: the same input and options give the same pixels, so the prompt of a generated image can
stay free and the look stays the same.

* :func:`pixelate` — nearest-neighbour downscale to a grid, colour key to transparency, colour
  quantisation (a fixed palette, or k-means from the image), an optional 1-px outline, and an
  optional nearest-neighbour upscale for display.
* :func:`sprite_sheet` — frames of one size in one horizontal strip, with the metadata a page
  needs (frame size, count, fps).
* :func:`css_animation` — the CSS ``steps()`` animation that plays such a strip.

Nothing here reads files or calls a model; the callers (the image actions of D126, the asset
checks of D114) hand in images and store the results."""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from PIL import Image

RGB = tuple[int, int, int]

# Two palettes of our own: a dark-to-light ramp, the primaries and secondaries, skin and earth.
# 32 adds a darker and a lighter step of the hues, so shading stays inside the palette.
PALETTES: dict[str, tuple[str, ...]] = {
    "basic16": (
        "#0b0b10", "#3a3a46", "#7c7c8a", "#c8c8d2", "#f4f4f0",
        "#a8323a", "#e0703a", "#f0c850", "#5aa040", "#2e6a50",
        "#3a6ac8", "#7ab8e8", "#7a4aa0", "#e08ab0", "#8a5a3a", "#e8b890",
    ),
    "basic32": (
        "#0b0b10", "#1e1e28", "#3a3a46", "#5a5a68", "#7c7c8a", "#a2a2ae", "#c8c8d2", "#f4f4f0",
        "#5a1a22", "#a8323a", "#e05a5a", "#8a3a1a", "#e0703a", "#f0a868", "#b08a28", "#f0c850",
        "#f8e8a0", "#2a5020", "#5aa040", "#a0d070", "#1a3a30", "#2e6a50", "#1e3a7a", "#3a6ac8",
        "#7ab8e8", "#3e2a5a", "#7a4aa0", "#b080d0", "#e08ab0", "#4a2e20", "#8a5a3a", "#e8b890",
    ),
}  # fmt: skip

MAX_GRID = 512  # pixel art is small; a bigger grid is a mistake, not a style
MAX_COLOURS = 64
KMEANS_ROUNDS = 12
# Sizes that bound memory, whatever a caller passes in: the source is checked before Pillow
# decodes it (a lazily opened file is only read by the first conversion), the results before they
# are allocated (a 512 grid at scale 64 would be 32768 x 32768 RGBA, 4 GiB).
MAX_SOURCE_PIXELS = 4096 * 4096
MAX_OUTPUT_SIDE = 4096
MAX_FRAMES = 256
MAX_SHEET_SIDE = 16384
MIN_FPS, MAX_FPS = 0.1, 60.0


class PixelArtError(ValueError):
    """The options cannot make pixel art (a grid of 0, an unknown palette, frames of mixed size)."""


def _whole(value: object, low: int, high: int, what: str) -> int:
    """``value`` as an int in ``low..high``; floats, strings and bools are refused."""
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise PixelArtError(f"{what} must be a whole number {low}..{high}")
    return value


@dataclass(frozen=True)
class SheetMeta:
    """What a page needs to play a sprite sheet (one horizontal strip)."""

    frame_width: int
    frame_height: int
    frames: int
    fps: float

    def __post_init__(self) -> None:
        if not (MIN_FPS <= self.fps <= MAX_FPS and math.isfinite(self.fps)):
            raise PixelArtError(f"fps must be {MIN_FPS:g}..{MAX_FPS:g}")
        _whole(self.frames, 1, MAX_FRAMES, "the number of frames")
        _whole(self.frame_width, 1, MAX_SHEET_SIDE, "the frame width")
        _whole(self.frame_height, 1, MAX_SHEET_SIDE, "the frame height")
        if self.width > MAX_SHEET_SIDE:
            raise PixelArtError(f"a sprite sheet is at most {MAX_SHEET_SIDE} pixels wide")

    @property
    def width(self) -> int:
        return self.frame_width * self.frames

    @property
    def duration_seconds(self) -> float:
        return self.frames / self.fps

    def as_dict(self) -> dict[str, float | int]:
        return {
            "frame_width": self.frame_width,
            "frame_height": self.frame_height,
            "frames": self.frames,
            "fps": self.fps,
            "width": self.width,
            "height": self.frame_height,
        }


def parse_colour(value: str | Sequence[int]) -> RGB:
    """``"#rrggbb"``, ``"rrggbb"``, ``"#rgb"`` or an ``(r, g, b)`` triple."""
    if isinstance(value, str):
        text = value.strip().removeprefix("#")
        if len(text) == 3:
            text = "".join(ch * 2 for ch in text)
        if not re.fullmatch(r"[0-9a-fA-F]{6}", text):
            raise PixelArtError(f"not a colour: {value!r}")
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    parts = tuple(int(v) for v in value)
    if len(parts) < 3 or not all(0 <= v <= 255 for v in parts[:3]):
        raise PixelArtError(f"not a colour: {value!r}")
    return (parts[0], parts[1], parts[2])


def palette_colours(palette: str | Sequence[str | Sequence[int]]) -> list[RGB]:
    """A named palette (:data:`PALETTES`) or a list of colours."""
    if isinstance(palette, str):
        if palette not in PALETTES:
            raise PixelArtError(f"unknown palette {palette!r} (known: {', '.join(PALETTES)})")
        return [parse_colour(c) for c in PALETTES[palette]]
    colours = [parse_colour(c) for c in palette]
    if not 1 <= len(colours) <= MAX_COLOURS:
        raise PixelArtError(f"a palette has 1 to {MAX_COLOURS} colours")
    return colours


def _distance(a: RGB, b: RGB) -> int:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def nearest(colour: RGB, palette: Sequence[RGB]) -> RGB:
    """The palette colour closest to ``colour`` (RGB distance; the first one on a tie)."""
    return min(palette, key=lambda p: _distance(colour, p))


def kmeans_palette(colours: Sequence[RGB], k: int, rounds: int = KMEANS_ROUNDS) -> list[RGB]:
    """``k`` colours that stand for ``colours``: Pillow's octree quantiser, refined by ``rounds``
    of k-means (in C, deterministic; median cut merged clearly different hues in a test). Fewer
    distinct colours than ``k`` are kept as they are. The same for one Pillow version (``uv.lock``
    pins it); another version may pick slightly different colours."""
    _whole(k, 1, MAX_COLOURS, "the number of k-means colours")
    distinct = sorted(set(colours))
    if len(distinct) <= k:
        return distinct
    strip = Image.new("RGB", (len(colours), 1))
    strip.frombytes(bytes(v for c in colours for v in c))
    quantised = strip.quantize(colors=k, method=Image.Quantize.FASTOCTREE, kmeans=rounds)
    flat = quantised.getpalette() or []
    used = sorted(i for _n, i in quantised.getcolors(k) or [])  # type: ignore[misc]
    return sorted({(flat[3 * i], flat[3 * i + 1], flat[3 * i + 2]) for i in used})


def _grid(size: int | tuple[int, int], source: tuple[int, int]) -> tuple[int, int]:
    """The target grid: ``(w, h)``, or one number for the longer side (the other keeps the
    image's proportions)."""
    what = "the grid"
    if isinstance(size, int) and not isinstance(size, bool):
        side = _whole(size, 1, MAX_GRID, what)
        w, h = source
        long = max(w, h)
        return (max(1, round(side * w / long)), max(1, round(side * h / long)))
    if not isinstance(size, tuple | list) or len(size) != 2:
        raise PixelArtError(f"{what} is (width, height) or one number")
    return (_whole(size[0], 1, MAX_GRID, what), _whole(size[1], 1, MAX_GRID, what))


def colour_key(image: Image.Image, key: str | Sequence[int], tolerance: int = 0) -> Image.Image:
    """``image`` as RGBA, with every pixel within ``tolerance`` (per channel) of ``key`` made fully
    transparent: the background of a generated sprite."""
    kr, kg, kb = parse_colour(key)
    out = image.convert("RGBA")
    px = out.load()
    assert px is not None  # noqa: S101 - load() of an image in memory always returns access
    for y in range(out.height):
        for x in range(out.width):
            r, g, b, _a = px[x, y]  # type: ignore[misc]
            if abs(r - kr) <= tolerance and abs(g - kg) <= tolerance and abs(b - kb) <= tolerance:
                px[x, y] = (0, 0, 0, 0)
    return out


def add_outline(image: Image.Image, colour: str | Sequence[int]) -> Image.Image:
    """A 1-px outline: every transparent pixel that touches an opaque one (left, right, up, down)
    takes ``colour``. The sprite itself is not changed."""
    rgb = parse_colour(colour)
    src = image.convert("RGBA")
    out = src.copy()
    sp, op = src.load(), out.load()
    assert sp is not None and op is not None  # noqa: S101
    w, h = src.size
    for y in range(h):
        for x in range(w):
            if sp[x, y][3] != 0:  # type: ignore[index]
                continue
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h and sp[nx, ny][3] != 0:  # type: ignore[index]
                    op[x, y] = (*rgb, 255)
                    break
    return out


def pixelate(
    image: Image.Image,
    grid: int | tuple[int, int],
    palette: str | Sequence[str | Sequence[int]] | None = "basic16",
    colours: int | None = None,
    outline: str | Sequence[int] | None = None,
    key: str | Sequence[int] | None = None,
    key_tolerance: int = 0,
    scale: int = 1,
) -> Image.Image:
    """Turn ``image`` into pixel art (RGBA).

    ``grid``: the size in art pixels, ``(w, h)`` or the longer side. ``palette``: a name of
    :data:`PALETTES` or a list of colours; ``colours`` instead picks that many colours from the
    image by k-means (``palette`` is then ignored); ``palette=None`` and no ``colours`` keep the
    colours. ``key``: the background colour that becomes transparent (with ``key_tolerance`` per
    channel). Semi-transparent pixels become fully opaque or fully transparent (alpha < 128).
    ``outline``: a 1-px outline colour around the opaque shape. ``scale``: a nearest-neighbour
    upscale of the result for display (1 = the grid itself).

    Limits: the source at most :data:`MAX_SOURCE_PIXELS` (checked before Pillow decodes it), the
    grid at most :data:`MAX_GRID` per side, grid times scale at most :data:`MAX_OUTPUT_SIDE`.
    The palette mapping runs in Python (up to a few seconds for a 512 grid of noise and 64
    colours), so an async caller runs it in a worker thread or process."""
    _whole(scale, 1, 64, "scale")
    _whole(key_tolerance, 0, 255, "the key tolerance")
    if image.width * image.height > MAX_SOURCE_PIXELS:
        raise PixelArtError(f"the image is larger than {MAX_SOURCE_PIXELS} pixels")
    size = _grid(grid, image.size)
    if max(size) * scale > MAX_OUTPUT_SIDE:
        raise PixelArtError(f"grid times scale must stay within {MAX_OUTPUT_SIDE} pixels")
    src = image.convert("RGBA")
    small = src.resize(size, Image.Resampling.NEAREST)
    if key is not None:  # pointwise, so after the nearest-neighbour pick: the same pixels, fewer
        small = colour_key(small, key, key_tolerance)
    raw = small.tobytes()
    pixels = [(raw[i], raw[i + 1], raw[i + 2], raw[i + 3]) for i in range(0, len(raw), 4)]
    opaque = [(r, g, b) for r, g, b, a in pixels if a >= 128]
    if colours is not None:
        chosen: list[RGB] | None = kmeans_palette(opaque, colours)
    elif palette is not None:
        chosen = palette_colours(palette)
    else:
        chosen = None
    cache: dict[RGB, RGB] = {}
    out = bytearray()
    for r, g, b, a in pixels:
        if a < 128:
            out += b"\0\0\0\0"
            continue
        rgb = (r, g, b)
        if chosen:
            if rgb not in cache:
                cache[rgb] = nearest(rgb, chosen)
            rgb = cache[rgb]
        out += bytes((*rgb, 255))
    art = Image.frombytes("RGBA", size, bytes(out))
    if outline is not None:
        art = add_outline(art, outline)
    if scale > 1:
        art = art.resize((size[0] * scale, size[1] * scale), Image.Resampling.NEAREST)
    return art


def sprite_sheet(frames: Sequence[Image.Image], fps: float = 8.0) -> tuple[Image.Image, SheetMeta]:
    """The frames side by side in one strip (left to right, in order), and its metadata. All
    frames must have the same size; at most :data:`MAX_FRAMES` frames, :data:`MAX_SHEET_SIDE`
    pixels wide, and :data:`MIN_FPS` to :data:`MAX_FPS` frames a second."""
    if not frames:
        raise PixelArtError("a sprite sheet needs at least one frame")
    w, h = frames[0].size
    if any(f.size != (w, h) for f in frames):
        raise PixelArtError("all frames of a sprite sheet must have the same size")
    meta = SheetMeta(frame_width=w, frame_height=h, frames=len(frames), fps=fps)  # checks limits
    sheet = Image.new("RGBA", (meta.width, h), (0, 0, 0, 0))
    for i, frame in enumerate(frames):
        sheet.paste(frame.convert("RGBA"), (i * w, 0))
    return sheet, meta


_CSS_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")
_CSS_URL = re.compile(r"[A-Za-z0-9/_.:?=&%-]{1,500}")


def css_animation(meta: SheetMeta, name: str, url: str, scale: int = 1) -> str:
    """CSS that plays the strip: a class ``.{name}`` of one frame's size and keyframes that step
    through the frames with ``steps()``, pixels kept sharp. ``name`` is a plain class name and
    ``url`` a plain path or URL (no quotes, brackets or spaces), so nothing can leave the rule."""
    if not _CSS_NAME.fullmatch(name):
        raise PixelArtError("the name must be lowercase letters, digits and '-'")
    if not _CSS_URL.fullmatch(url):
        raise PixelArtError("the url may hold letters, digits and / _ . : ? = & % - only")
    _whole(scale, 1, 64, "scale")
    fw, fh = meta.frame_width * scale, meta.frame_height * scale
    total = fw * meta.frames
    duration = f"{meta.duration_seconds:.3f}".rstrip("0").rstrip(".")
    return (
        f".{name} {{\n"
        f"  width: {fw}px;\n"
        f"  height: {fh}px;\n"
        f'  background: url("{url}") 0 0 / {total}px {fh}px no-repeat;\n'
        "  image-rendering: pixelated;\n"
        f"  animation: {name}-play {duration}s steps({meta.frames}) infinite;\n"
        "}\n"
        f"@keyframes {name}-play {{\n"
        "  from { background-position: 0 0; }\n"
        f"  to {{ background-position: -{total}px 0; }}\n"
        "}\n"
    )
