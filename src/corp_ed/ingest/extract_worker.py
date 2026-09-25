"""Дочерний процесс извлечения текста: python -I -m corp_ed.ingest.extract_worker FMT

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
CPU_SECONDS = 60


def _limit_resources() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    # Запись файлов запрещена целиком: парсеру она не нужна.
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    # Core dump содержал бы документ клиента.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def main() -> int:
    _limit_resources()

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
