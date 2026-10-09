"""Фото профиля и логотип компании: картинка → WebP 256×256 (ТЗ §4, §7).

Загруженный файл не хранится как есть: Pillow открывает его (с потолком
на число пикселей — защита от «бомб» распаковки), поворачивает по EXIF и
перекодирует в WebP. Метаданные снимка — геометка, модель телефона,
дата — не переживают перекодирование.

Декодеры Pillow (libjpeg, libpng, libwebp) написаны на C и читают файл,
присланный пользователем, поэтому этот модуль работает только в дочернем
процессе песочницы (extract_worker.py, режимы avatar и logo); из API —
через sandbox.process_image_isolated. Импортирует только Pillow и
стандартную библиотеку: дочерний процесс стартует на каждую картинку.
"""

import io
from enum import StrEnum

from PIL import Image, ImageOps, UnidentifiedImageError

AVATAR_SIZE = 256
LOGO_SIZE = 256
MAX_PIXELS = 40_000_000
"""40 Мп: снимок современного телефона проходит, «бомба» на гигапиксели —
нет: размер из заголовка проверяется до распаковки."""
ACCEPTED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})


class ImageKind(StrEnum):
    AVATAR = "avatar"
    LOGO = "logo"


class ImageError(Exception):
    """Картинку нельзя принять. code: `invalid` — не JPEG, PNG или WebP,
    битая или песочница не справилась; `too_many_pixels` — больше
    MAX_PIXELS; `sandbox_unavailable` — песочница отказалась работать
    (sandbox.py). Тексты для человека — у сервисов."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


IMAGE_ERROR_CODES = frozenset({"invalid", "too_many_pixels", "sandbox_unavailable"})


def open_image(raw: bytes) -> Image.Image:
    """Открыть присланную картинку: только JPEG, PNG, WebP, не больше
    MAX_PIXELS, с поворотом по EXIF."""
    try:
        # open читает только заголовок: размер проверяем до распаковки.
        # Глобальный Image.MAX_IMAGE_PIXELS не трогаем — им пользуются и
        # разборщики документов.
        with Image.open(io.BytesIO(raw)) as source:
            if source.format not in ACCEPTED_FORMATS:
                raise ImageError("invalid")
            width, height = source.size
            if width * height > MAX_PIXELS:
                raise ImageError("too_many_pixels")
            image = ImageOps.exif_transpose(source)
            image.load()
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        SyntaxError,
    ) as exc:
        raise ImageError("invalid") from exc
    return image


def avatar(raw: bytes) -> bytes:
    """Файл → квадрат 256×256 WebP без метаданных."""
    image = open_image(raw)
    # Прозрачность — на белый фон: в списке коллег фото на любой теме
    # должно читаться одинаково.
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.getchannel("A"))
        image = background
    else:
        image = image.convert("RGB")
    square = ImageOps.fit(
        image, (AVATAR_SIZE, AVATAR_SIZE), method=Image.Resampling.LANCZOS
    )
    out = io.BytesIO()
    square.save(out, format="WEBP", quality=85, method=6)
    return out.getvalue()


def logo(raw: bytes) -> bytes:
    """Картинка → вписана в квадрат 256×256 без обрезки, прозрачный фон,
    WebP без метаданных. Логотипы бывают вытянутыми — обрезать их нельзя."""
    image = open_image(raw).convert("RGBA")
    image = ImageOps.contain(
        image, (LOGO_SIZE, LOGO_SIZE), method=Image.Resampling.LANCZOS
    )
    square = Image.new("RGBA", (LOGO_SIZE, LOGO_SIZE), (0, 0, 0, 0))
    square.paste(
        image, ((LOGO_SIZE - image.width) // 2, (LOGO_SIZE - image.height) // 2)
    )
    out = io.BytesIO()
    square.save(out, format="WEBP", quality=90, method=6)
    return out.getvalue()


def process(kind: ImageKind, raw: bytes) -> bytes:
    """Вызывать в песочнице (extract_worker.py)."""
    return avatar(raw) if kind is ImageKind.AVATAR else logo(raw)
