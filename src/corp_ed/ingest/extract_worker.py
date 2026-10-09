"""Дочерний процесс разбора присланных файлов: документов и картинок.

Запуск: python -I -m corp_ed.ingest.extract_worker MODE [CPU_SECONDS [NETWORK]]

MODE — формат документа (SourceFormat) или `avatar` / `logo` (images.py).
Читает файл из stdin, пишет JSON в stdout: для документа
{"ok": true, "markdown": …}, для картинки {"ok": true, "image": WebP в
base64}; при ошибке {"ok": false, "code": …}. Запускается только из
ingest/sandbox.py.

До импорта парсеров процесс сам себя ограничивает: закрывает себе сеть
(seccomp, no_network.py), уходит в пустой удалённый каталог и урезает
ресурсы: память, CPU и запись файлов. Если в PDFium или в разборе XML
найдётся ошибка, через которую файл получит управление процессом, он
окажется без секретов в окружении, без сети, без права писать на диск и
с потолком памяти и времени.

NETWORK — что делать, если фильтр сети не ставится: `strict` (production)
— отказаться от разбора, ответ {"ok": false, "code": "sandbox_unavailable"};
`lenient` (разработка) — разбирать, в ответе "network": "open".
"""

import base64
import json
import os
import resource
import sys
import tempfile

MEMORY_LIMIT = 1536 * 1024 * 1024
# Потолок CPU передаёт sandbox.py: таймаут по стене на число ядер. Лимит
# здесь — вторая линия на случай, если родитель не убил процесс по
# таймауту.
DEFAULT_CPU_SECONDS = 600
STRICT = "strict"
LENIENT = "lenient"


def _limit_resources(cpu_seconds: int) -> None:
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    # Запись файлов запрещена целиком: парсеру она не нужна.
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    # Core dump содержал бы документ клиента.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _leave_working_directory() -> None:
    """Рабочий каталог — пустой и сразу удалённый: относительный путь в
    файле клиента никуда не ведёт, создать в нём ничего нельзя, и на
    диске после процесса ничего не остаётся."""
    try:
        path = tempfile.mkdtemp(prefix="kronto-extract-")
        os.chdir(path)
        os.rmdir(path)
    except OSError:
        os.chdir("/")


def _confine() -> bool:
    """Закрыть себе сеть и рабочий каталог. False — сеть не закрыта."""
    _leave_working_directory()
    from corp_ed.ingest.no_network import NetworkFilterError, deny_network

    try:
        deny_network()
    except NetworkFilterError as exc:
        # Причина — в журнал родителя (хвост stderr), наружу — только код.
        sys.stderr.write(f"network filter not installed: {exc}\n")
        return False
    return True


_IMAGE_MODES = ("avatar", "logo")


def _document(mode: str) -> dict[str, object]:
    from corp_ed.ingest.extract import ExtractionError, SourceFormat, extract

    try:
        fmt = SourceFormat(mode)
        data = sys.stdin.buffer.read()
        return {"ok": True, "markdown": extract(fmt, data)}
    except ExtractionError as exc:
        return {"ok": False, "code": exc.code}
    except MemoryError:
        return {"ok": False, "code": "document_too_large"}
    except Exception:  # noqa: BLE001 — наружу только код, без трассировки
        return {"ok": False, "code": "corrupted"}


def _image(mode: str) -> dict[str, object]:
    # Только Pillow: разборщики документов и настройки картинке не нужны,
    # а процесс стартует на каждую загрузку фото.
    from corp_ed.ingest.images import ImageError, ImageKind, process

    try:
        webp = process(ImageKind(mode), sys.stdin.buffer.read())
    except ImageError as exc:
        return {"ok": False, "code": exc.code}
    except Exception:  # noqa: BLE001 — и MemoryError: картинка не читается
        return {"ok": False, "code": "invalid"}
    return {"ok": True, "image": base64.b64encode(webp).decode("ascii")}


def main() -> int:
    cpu_seconds = DEFAULT_CPU_SECONDS
    if len(sys.argv) > 2 and sys.argv[2].isdigit():
        cpu_seconds = max(1, int(sys.argv[2]))
    # Без явного lenient — строго: запуск без аргумента не должен молча
    # разбирать файл с открытой сетью.
    strict = len(sys.argv) <= 3 or sys.argv[3] != LENIENT
    # Фильтр — первым, пока процесс однопоточный и ничего не прочитал.
    confined = _confine()
    _limit_resources(cpu_seconds)
    if not confined and strict:
        sys.stdout.write(json.dumps({"ok": False, "code": "sandbox_unavailable"}))
        return 0

    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    result = _image(mode) if mode in _IMAGE_MODES else _document(mode)
    if not confined:
        result["network"] = "open"
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
