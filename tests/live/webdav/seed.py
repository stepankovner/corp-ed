"""Сетевой диск выдуманной компании для живой проверки вида `webdav`.

Дерево одно на всех (как общий ресурс NAS): «Общие» видят оба
пользователя, «Кадры» — только ivan (dav.conf). Файлы — docx, pdf, md,
txt и неподдерживаемый png, вложенность — до трёх уровней. Ожидания для
tests/live/test_webdav_live.py — здесь же (EXPECTED).

    WEBDAV_URL=https://127.0.0.1:8443/dav/ DAV_STAND_CA=~/dav-tls/cert.pem \\
        python -m tests.live.webdav.seed

Повторный запуск перезаписывает те же файлы — стенд возвращается к
исходному виду (тесты меняют и удаляют файлы и восстанавливают их сами).
"""

import os
import sys

from tests.ingest import samples
from tests.live.dav_stand import PASSWORD, DavSeeder

USERS = ("ivan", "maria")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
"""Не картинка, но формат по имени и сигнатуре — png: ingest его не читает."""

FILES: dict[str, bytes] = {
    "Общие/Регламент отпусков.docx": samples.docx(
        [("Регламент отпусков", "Heading1"), ("Отпуск — 28 календарных дней.", None)]
    ),
    "Общие/Политики/Security policy.pdf": samples.pdf(
        [[("Security policy", 20), ("Passwords are rotated every 90 days.", 12)]]
    ),
    "Общие/Политики/Удалённая работа/Правила.md": (
        "# Удалённая работа\n\nДва дня в неделю — из дома.\n".encode()
    ),
    "Общие/Глоссарий.txt": "Глоссарий: ДМС — добровольное медстрахование.\n".encode(),
    "Общие/Схема офиса.png": PNG,
    "Кадры/Зарплаты.md": "# Зарплаты\n\nОклады пересматриваются в марте.\n".encode(),
    "Кадры/Премии/Премии 2026.txt": "Премия — до 20 % оклада.\n".encode(),
}

EXPECTED: dict[str, set[str]] = {
    "Регламент отпусков.docx": {"ivan", "maria"},
    "Security policy.pdf": {"ivan", "maria"},
    "Правила.md": {"ivan", "maria"},
    "Глоссарий.txt": {"ivan", "maria"},
    "Зарплаты.md": {"ivan"},
    "Премии 2026.txt": {"ivan"},
}
"""Название → кто видит. Схема офиса.png не индексируется (формат)."""
TEXT = {
    "Регламент отпусков.docx": "28 календарных дней",
    "Security policy.pdf": "every 90 days",
    "Правила.md": "Два дня в неделю",
    "Глоссарий.txt": "ДМС",
    "Зарплаты.md": "в марте",
    "Премии 2026.txt": "до 20 %",
}
"""Что должно дойти до текста материала после разбора."""


def seed(url: str) -> None:
    owner = DavSeeder(url, "ivan", PASSWORD)
    try:
        for path, data in FILES.items():
            owner.put(path, data)
    finally:
        owner.close()


if __name__ == "__main__":
    address = os.environ.get("WEBDAV_URL", "")
    if not address:
        sys.exit("нужен WEBDAV_URL (https://127.0.0.1:8443/dav/)")
    seed(address)
    print(f"файлов: {len(FILES)}")
