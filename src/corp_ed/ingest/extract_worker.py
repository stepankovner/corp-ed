"""Дочерний процесс извлечения текста.

Запуск: python -I -m corp_ed.ingest.extract_worker FMT [CPU_SECONDS [NETWORK]]

Читает файл из stdin, пишет JSON в stdout: {"ok": true, "markdown": …}
или {"ok": false, "code": …}. Запускается только из ingest/sandbox.py.

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

    from corp_ed.ingest.extract import ExtractionError, SourceFormat, extract

    result: dict[str, object]
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

    if not confined:
        result["network"] = "open"
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
