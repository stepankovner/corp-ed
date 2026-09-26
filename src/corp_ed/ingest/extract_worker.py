"""Дочерний процесс извлечения текста.

Запуск: python -I -m corp_ed.ingest.extract_worker FMT [CPU_SECONDS]

Читает файл из stdin, пишет JSON в stdout: {"ok": true, "markdown": …}
или {"ok": false, "code": …}. Запускается только из ingest/sandbox.py.

До импорта парсеров процесс сам себе урезает ресурсы: память, CPU и
запись файлов. Если в MuPDF или в разборе XML найдётся уязвимость,
эксплойт окажется в процессе без секретов в окружении, без права
писать на диск и с потолком памяти и времени.
"""

import json
import resource
import sys

MEMORY_LIMIT = 1536 * 1024 * 1024
# Потолок CPU передаёт sandbox.py: таймаут по стене на число ядер.
# Разбор PDF многопоточный (модель разметки pymupdf-layout на
# onnxruntime занимает все ядра: 85 страниц — 23 с по стене и 75 с CPU
# на 4 ядрах), и фиксированные 60 с CPU убивали честный документ раньше
# таймаута. Лимит здесь — вторая линия на случай, если родитель не
# убил процесс по таймауту.
DEFAULT_CPU_SECONDS = 600


def _limit_resources(cpu_seconds: int) -> None:
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    # Запись файлов запрещена целиком: парсеру она не нужна.
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    # Core dump содержал бы документ клиента.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def main() -> int:
    cpu_seconds = DEFAULT_CPU_SECONDS
    if len(sys.argv) > 2 and sys.argv[2].isdigit():
        cpu_seconds = max(1, int(sys.argv[2]))
    _limit_resources(cpu_seconds)

    from corp_ed.ingest.extract import ExtractionError, SourceFormat, extract

    try:
        fmt = SourceFormat(sys.argv[1])
        data = sys.stdin.buffer.read()
        result = {"ok": True, "markdown": extract(fmt, data)}
    except ExtractionError as exc:
        result = {"ok": False, "code": exc.code}
    except MemoryError:
        result = {"ok": False, "code": "document_too_large"}
    except Exception:  # noqa: BLE001 — наружу только код, без трассировки
        result = {"ok": False, "code": "corrupted"}

    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
