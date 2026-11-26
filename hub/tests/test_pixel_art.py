"""Pixel art and sprite sheets (D126), pure functions over small images made in the test.

The golden images are character grids: one character per art pixel, a letter per palette colour
(``A`` = the first colour of the palette …) and ``.`` for a transparent pixel."""

import pytest
from PIL import Image, ImageDraw

from topcoat_hub import pixel_art as pa

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"


def grid(image: Image.Image, palette: str = "basic16") -> list[str]:
    legend = {(0, 0, 0, 0): "."}
    for i, colour in enumerate(pa.palette_colours(palette)):
        legend[(*colour, 255)] = LETTERS[i]
    return [
        "".join(legend[image.getpixel((x, y))] for x in range(image.width))  # type: ignore[index]
        for y in range(image.height)
    ]


def sprite() -> Image.Image:
    """32 x 32: a red block with a yellow corner and a navy band, on a magenta key colour."""
    src = Image.new("RGB", (32, 32), "#ff00ff")
    d = ImageDraw.Draw(src)
    d.rectangle([8, 8, 23, 23], fill="#c83c3c")
    d.rectangle([8, 8, 15, 15], fill="#f0d060")
    d.rectangle([12, 20, 19, 23], fill="#203060")
    return src


def colours_of(image: Image.Image) -> set[tuple[int, ...]]:
    return {c for _n, c in image.getcolors(1 << 16) or []}  # type: ignore[misc]


# --- pixelate ---------------------------------------------------------------------------------


def test_a_sprite_on_a_key_colour_becomes_small_outlined_pixel_art_on_transparency() -> None:
    art = pa.pixelate(sprite(), 8, "basic16", outline="#0b0b10", key="#ff00ff")
    assert art.size == (8, 8) and art.mode == "RGBA"
    assert grid(art) == [
        "........",
        "..AAAA..",
        ".AHHFFA.",
        ".AHHFFA.",
        ".AFFFFA.",
        ".AFBBFA.",
        "..AAAA..",
        "........",
    ]
    # the same input and options give the same pixels
    again = pa.pixelate(sprite(), 8, "basic16", outline="#0b0b10", key="#ff00ff")
    assert again.tobytes() == art.tobytes()


def test_without_a_key_nothing_is_transparent_and_without_an_outline_nothing_is_added() -> None:
    art = pa.pixelate(sprite(), 8, "basic16")
    rows = grid(art)
    assert "." not in "".join(rows) and "A" not in "".join(rows)
    assert rows[0] == "NNNNNNNN"  # magenta comes out as the palette's pink


def test_the_scale_upscales_with_hard_edges() -> None:
    art = pa.pixelate(sprite(), 8, "basic16", key="#ff00ff", scale=4)
    assert art.size == (32, 32)
    small = pa.pixelate(sprite(), 8, "basic16", key="#ff00ff")
    for y in range(32):
        for x in range(32):
            assert art.getpixel((x, y)) == small.getpixel((x // 4, y // 4))


def test_one_number_is_the_longer_side_and_the_proportions_stay() -> None:
    wide = Image.new("RGB", (64, 32), "#3a6ac8")
    assert pa.pixelate(wide, 16).size == (16, 8)
    assert pa.pixelate(wide, (5, 7)).size == (5, 7)
    for bad in (0, (0, 4), (pa.MAX_GRID + 1, 4)):
        with pytest.raises(pa.PixelArtError):
            pa.pixelate(wide, bad)


def test_every_pixel_lands_on_the_palette_and_a_list_of_colours_is_a_palette() -> None:
    gradient = Image.new("RGB", (64, 1))
    for x in range(64):
        gradient.putpixel((x, 0), (x * 4, 255 - x * 4, 128))
    for name in ("basic16", "basic32"):
        allowed = {(*c, 255) for c in pa.palette_colours(name)}
        assert colours_of(pa.pixelate(gradient, 64, name)) <= allowed
    two = pa.pixelate(gradient, 64, ["#000", "#ffffff"])
    assert colours_of(two) <= {(0, 0, 0, 255), (255, 255, 255, 255)}


def test_the_named_palettes_have_16_and_32_different_colours() -> None:
    assert len(set(pa.palette_colours("basic16"))) == 16
    assert len(set(pa.palette_colours("basic32"))) == 32
    with pytest.raises(pa.PixelArtError):
        pa.palette_colours("nope")
    with pytest.raises(pa.PixelArtError):
        pa.palette_colours(["#12345"])


def test_k_means_takes_its_colours_from_the_image_deterministically() -> None:
    """Three areas, each with a little noise: three colours come out, near the three areas."""
    src = Image.new("RGB", (30, 10))
    base = [(200, 40, 40), (40, 200, 40), (40, 40, 200)]
    for x in range(30):
        for y in range(10):
            r, g, b = base[x // 10]
            noise = (x * 7 + y * 3) % 5 - 2
            src.putpixel((x, y), (r + noise, g + noise, b + noise))
    art = pa.pixelate(src, (30, 10), colours=3)
    found = sorted(c[:3] for c in colours_of(art))
    assert len(found) == 3
    for got, want in zip(found, sorted(base), strict=True):
        assert all(abs(a - b) <= 2 for a, b in zip(got, want, strict=True)), (got, want)
    assert pa.pixelate(src, (30, 10), colours=3).tobytes() == art.tobytes()
    # fewer colours in the image than asked for: they are kept as they are
    flat = Image.new("RGB", (4, 4), (10, 20, 30))
    assert colours_of(pa.pixelate(flat, 4, colours=8)) == {(10, 20, 30, 255)}


def test_without_a_palette_the_downscale_brings_no_new_colours() -> None:
    src = sprite()
    art = pa.pixelate(src, 8, palette=None)
    assert {c[:3] for c in colours_of(art)} <= {c[:3] for c in colours_of(src.convert("RGBA"))}


def test_half_transparent_pixels_become_opaque_or_transparent() -> None:
    src = Image.new("RGBA", (2, 1))
    src.putpixel((0, 0), (200, 40, 40, 127))
    src.putpixel((1, 0), (200, 40, 40, 128))
    art = pa.pixelate(src, (2, 1), "basic16")
    assert art.getpixel((0, 0)) == (0, 0, 0, 0)
    assert art.getpixel((1, 0))[3] == 255  # type: ignore[index]


def test_sizes_are_bounded_before_anything_is_decoded_or_allocated() -> None:
    """A huge source is refused on its size alone (``Image.open`` is lazy, the first conversion
    would decode it), and so is a result that grid times scale would make huge."""
    lazy = Image.new("1", (pa.MAX_SOURCE_PIXELS // 1024 + 1, 1024))
    with pytest.raises(pa.PixelArtError):
        pa.pixelate(lazy, 8)
    small = Image.new("RGB", (8, 8))
    with pytest.raises(pa.PixelArtError):
        pa.pixelate(small, pa.MAX_GRID, scale=pa.MAX_OUTPUT_SIDE // pa.MAX_GRID + 1)
    assert pa.pixelate(small, 64, scale=pa.MAX_OUTPUT_SIDE // 64).size == (4096, 4096)


@pytest.mark.parametrize(
    "options",
    [
        {"grid": 8.0},
        {"grid": (2.5, 3)},
        {"grid": (True, 4)},
        {"grid": "8"},
        {"grid": (3, 4, 5)},
        {"grid": 8, "scale": 2.0},
        {"grid": 8, "colours": 2.5},
        {"grid": 8, "key": "#000", "key_tolerance": 1.5},
    ],
)
def test_options_that_are_not_whole_numbers_are_refused_plainly(options: dict) -> None:
    with pytest.raises(pa.PixelArtError):
        pa.pixelate(Image.new("RGB", (8, 8)), **options)


def test_k_means_on_a_fully_transparent_image_gives_a_transparent_image() -> None:
    art = pa.pixelate(Image.new("RGBA", (4, 4)), 4, colours=4, outline="#000")
    assert colours_of(art) == {(0, 0, 0, 0)}


def test_the_key_tolerance_takes_a_noisy_background_too() -> None:
    src = Image.new("RGB", (2, 1), "#ff00ff")
    src.putpixel((1, 0), (250, 6, 250))
    assert pa.pixelate(src, (2, 1), key="#ff00ff").getpixel((1, 0))[3] == 255  # type: ignore[index]
    keyed = pa.pixelate(src, (2, 1), key="#ff00ff", key_tolerance=8)
    assert keyed.getpixel((1, 0)) == (0, 0, 0, 0)


# --- sprite sheets and their animation ----------------------------------------------------------


def frame(colour: str) -> Image.Image:
    return pa.pixelate(Image.new("RGB", (4, 4), colour), 4, "basic16")


def test_frames_go_side_by_side_in_order_with_their_metadata() -> None:
    frames = [frame("#a8323a"), frame("#5aa040"), frame("#3a6ac8")]
    sheet, meta = pa.sprite_sheet(frames, fps=6)
    assert sheet.size == (12, 4)
    assert grid(sheet) == ["FFFFIIIIKKKK"] * 4
    assert meta.as_dict() == {
        "frame_width": 4,
        "frame_height": 4,
        "frames": 3,
        "fps": 6,
        "width": 12,
        "height": 4,
    }
    assert meta.duration_seconds == pytest.approx(0.5)


def test_a_sprite_sheet_refuses_frames_of_mixed_size_and_bad_fps() -> None:
    with pytest.raises(pa.PixelArtError):
        pa.sprite_sheet([frame("#a8323a"), pa.pixelate(Image.new("RGB", (4, 4)), (4, 3))])
    with pytest.raises(pa.PixelArtError):
        pa.sprite_sheet([])
    for fps in (0, -1, 61, float("nan"), float("inf"), 1e-310):
        with pytest.raises(pa.PixelArtError):
            pa.sprite_sheet([frame("#a8323a")], fps=fps)


def test_a_sprite_sheet_is_bounded_in_frames_and_width() -> None:
    with pytest.raises(pa.PixelArtError):
        pa.sprite_sheet([frame("#a8323a")] * (pa.MAX_FRAMES + 1))
    wide = Image.new("RGBA", (pa.MAX_SHEET_SIDE // 2 + 1, 1))
    with pytest.raises(pa.PixelArtError):
        pa.sprite_sheet([wide, wide])
    # metadata made by hand is held to the same rules, so the CSS never says "infs"
    with pytest.raises(pa.PixelArtError):
        pa.SheetMeta(frame_width=4, frame_height=4, frames=2, fps=1e-310)


def test_the_css_steps_through_the_frames() -> None:
    _, meta = pa.sprite_sheet([frame("#a8323a")] * 4, fps=8)
    css = pa.css_animation(meta, "hero-walk", "/api/files/abc.png", scale=3)
    assert css == (
        ".hero-walk {\n"
        "  width: 12px;\n"
        "  height: 12px;\n"
        '  background: url("/api/files/abc.png") 0 0 / 48px 12px no-repeat;\n'
        "  image-rendering: pixelated;\n"
        "  animation: hero-walk-play 0.5s steps(4) infinite;\n"
        "}\n"
        "@keyframes hero-walk-play {\n"
        "  from { background-position: 0 0; }\n"
        "  to { background-position: -48px 0; }\n"
        "}\n"
    )


@pytest.mark.parametrize(
    "name, url",
    [
        ("Hero", "/a.png"),  # not a plain lowercase class name
        ("x{}", "/a.png"),
        ("hero", '/a.png") ; } body { display: none'),  # would leave the rule
        ("hero", "/a b.png"),
        ("hero", "javascript:alert(1)"),
    ],
)
def test_the_css_takes_only_plain_names_and_urls(name: str, url: str) -> None:
    _, meta = pa.sprite_sheet([frame("#a8323a")])
    with pytest.raises(pa.PixelArtError):
        pa.css_animation(meta, name, url)
