"""Фото профиля и логотип: декодирование в дочернем процессе песочницы.

Правила те же, что были в процессе API: JPEG, PNG, WebP; не больше 40 Мп
по заголовку; поворот по EXIF; на выходе — WebP 256×256. Любой сбой
дочернего процесса — та же ошибка, что для битой картинки.
"""

import io
import sys
from types import SimpleNamespace

import pytest
from PIL import Image
from structlog.testing import capture_logs

from corp_ed.ingest import images, sandbox
from corp_ed.ingest.images import ImageError, ImageKind
from corp_ed.ingest.sandbox import process_image_isolated


def _encode(image: Image.Image, fmt: str, **save: object) -> bytes:
    out = io.BytesIO()
    image.save(out, format=fmt, **save)
    return out.getvalue()


def _photo(size: tuple[int, int] = (800, 600), fmt: str = "JPEG") -> bytes:
    image = Image.new("RGB", size, (200, 30, 30))
    for x in range(0, size[0], 5):
        image.putpixel((x, (3 * x) % size[1]), (x % 256, 90, 200))
    return _encode(image, fmt)


def _rotated_jpeg() -> bytes:
    """Снимок 640×320, который по EXIF надо повернуть на 90°."""
    exif = Image.Exif()
    exif[0x0112] = 6
    return _encode(Image.new("RGB", (640, 320), (10, 120, 200)), "JPEG", exif=exif)


async def _code(kind: ImageKind, raw: bytes) -> str:
    with pytest.raises(ImageError) as info:
        await process_image_isolated(kind, raw)
    return info.value.code


@pytest.mark.parametrize("kind", list(ImageKind))
@pytest.mark.parametrize(
    "raw",
    [
        _photo(),
        _photo((300, 100), "PNG"),
        _encode(Image.new("RGBA", (300, 500), (0, 0, 0, 0)), "PNG"),
        _encode(Image.new("P", (120, 90), 3), "PNG"),
        _photo((640, 480), "WEBP"),
        _rotated_jpeg(),
    ],
    ids=["jpeg", "png", "transparent", "palette", "webp", "exif-rotated"],
)
async def test_child_output_matches_in_process_processing(
    kind: ImageKind, raw: bytes
) -> None:
    """В песочнице — тот же Pillow и тот же код: байт в байт как без неё."""
    expected = images.process(kind, raw)
    result = await process_image_isolated(kind, raw)
    assert result == expected
    with Image.open(io.BytesIO(result)) as picture:
        assert picture.format == "WEBP"
        assert picture.size == (256, 256)


async def test_avatar_is_turned_by_exif_and_cropped() -> None:
    result = await process_image_isolated(ImageKind.AVATAR, _rotated_jpeg())
    with Image.open(io.BytesIO(result)) as picture:
        assert picture.mode == "RGB"
        assert not picture.getexif()


async def test_logo_keeps_transparent_margins() -> None:
    wide = _encode(Image.new("RGBA", (300, 100), (20, 20, 20, 255)), "PNG")
    result = await process_image_isolated(ImageKind.LOGO, wide)
    with Image.open(io.BytesIO(result)) as picture:
        logo = picture.convert("RGBA")
        assert logo.getpixel((128, 2))[3] == 0
        assert logo.getpixel((128, 128))[3] == 255


@pytest.mark.parametrize("kind", list(ImageKind))
@pytest.mark.parametrize(
    "raw",
    [
        b"%PDF-1.7 not an image",
        b"\xff\xd8\xff\xe0 broken jpeg",
        _photo()[:400],
        _encode(Image.new("RGB", (64, 64)), "GIF"),
        _encode(Image.new("RGB", (64, 64)), "BMP"),
    ],
    ids=["pdf", "broken-header", "truncated", "gif", "bmp"],
)
async def test_non_images_and_other_formats_are_invalid(
    kind: ImageKind, raw: bytes
) -> None:
    assert await _code(kind, raw) == "invalid"


async def test_giant_dimensions_are_refused_before_decoding() -> None:
    bomb = _encode(Image.new("1", (8000, 6000)), "PNG")
    assert await _code(ImageKind.AVATAR, bomb) == "too_many_pixels"
    assert await _code(ImageKind.LOGO, bomb) == "too_many_pixels"


async def test_largest_allowed_photo_fits_into_child_limits() -> None:
    """39 Мп — под потолком 40 Мп: распаковка в RGB, поворот и уменьшение
    укладываются в потолок памяти дочернего процесса."""
    photo = _encode(Image.new("RGB", (7200, 5400), (90, 140, 30)), "JPEG")
    # Самый дорогой путь: прозрачность — копии RGBA, белый фон и маска.
    transparent = _encode(Image.new("RGBA", (7200, 5400), (90, 140, 30, 128)), "PNG")
    for raw in (photo, transparent):
        for kind in ImageKind:
            result = await process_image_isolated(kind, raw)
            with Image.open(io.BytesIO(result)) as picture:
                assert picture.size == (256, 256)


# --- сбои дочернего процесса ---------------------------------------------------


def _fake_worker(monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    monkeypatch.setattr(
        sandbox,
        "_worker_command",
        lambda mode, cpu_seconds: [sys.executable, "-I", "-c", script],
    )


@pytest.mark.parametrize(
    "script",
    [
        # Упал в C-коде декодера.
        "import os, signal; os.kill(os.getpid(), signal.SIGSEGV)",
        # Убит по памяти или CPU.
        "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
        "raise SystemExit(1)",
        "print('not json')",
        "import json; print(json.dumps([1]))",
        "import json; print(json.dumps({'ok': True, 'image': 'не base64!'}))",
        "import json; print(json.dumps({'ok': True, 'markdown': 'текст'}))",
        "import json; print(json.dumps({'ok': False, 'code': 'whatever'}))",
    ],
    ids=[
        "segfault",
        "killed",
        "exit-1",
        "not-json",
        "not-object",
        "bad-base64",
        "no-image",
        "unknown-code",
    ],
)
async def test_child_failure_is_an_invalid_image(
    monkeypatch: pytest.MonkeyPatch, script: str
) -> None:
    _fake_worker(monkeypatch, script)
    assert await _code(ImageKind.AVATAR, _photo()) == "invalid"


async def test_slow_child_is_killed_as_invalid_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_worker(monkeypatch, "import time; time.sleep(60)")
    with pytest.raises(ImageError) as info:
        await process_image_isolated(ImageKind.LOGO, _photo(), timeout=0.5)
    assert info.value.code == "invalid"


async def test_flooding_child_is_killed_as_invalid_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "MAX_OUTPUT_BYTES", 1024 * 1024)
    _fake_worker(
        monkeypatch,
        "import sys, time\n"
        "sys.stdout.buffer.write(b'x' * (2 * 1024 * 1024))\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n",
    )
    assert await _code(ImageKind.AVATAR, _photo()) == "invalid"


def test_image_answer_fits_output_ceiling() -> None:
    """WebP 256×256 в base64 намного меньше потолка ответа песочницы."""
    noise = Image.effect_noise((2000, 2000), 120).convert("RGB")
    worst = images.process(ImageKind.AVATAR, _encode(noise, "PNG"))
    assert 4 * len(worst) // 3 + 1024 < sandbox.MAX_OUTPUT_BYTES


# --- под фильтром сети ----------------------------------------------------------


async def test_images_are_processed_under_network_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != "linux":
        pytest.skip("фильтр seccomp — только Linux")
    monkeypatch.setattr(
        sandbox, "get_settings", lambda: SimpleNamespace(is_production=True)
    )
    with capture_logs() as logs:
        for kind in ImageKind:
            assert await process_image_isolated(kind, _photo())
    assert not [entry for entry in logs if entry["event"].startswith("sandbox_")]


async def test_image_child_without_filter_refuses_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sandbox, "get_settings", lambda: SimpleNamespace(is_production=True)
    )
    _fake_worker(
        monkeypatch,
        "import sys\n"
        "from corp_ed.ingest import extract_worker, no_network\n"
        "def broken():\n"
        "    raise no_network.NetworkFilterError('test: no seccomp')\n"
        "no_network.deny_network = broken\n"
        "sys.argv = ['extract_worker', 'avatar', '10', 'strict']\n"
        "sys.exit(extract_worker.main())\n",
    )
    assert await _code(ImageKind.AVATAR, _photo()) == "sandbox_unavailable"
