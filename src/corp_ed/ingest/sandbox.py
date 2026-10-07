"""Запуск извлечения текста в отдельном процессе с ограничениями.

Почему не в процессе API:
- парсеры PDF и docx обрабатывают файл, присланный клиентом; PDFium
  (pypdfium2) написан на C++, и уязвимость в нём — выполнение кода с правами API,
  где в памяти SECRET_KEY, ключ Yandex Cloud и соединения с базой;
- «PDF-бомба» или бесконечный цикл в парсере занимали бы воркер API;
- разбор PDF — секунды CPU; в event loop он остановил бы все запросы.

Дочерний процесс получает пустое окружение (кроме PATH и локали),
урезает себе память, CPU и запись файлов (extract_worker.py) и убивается
по таймауту. Наружу — только Markdown или код ошибки; ответ больше
MAX_OUTPUT_BYTES не дочитывается — процесс убивается.
"""

import asyncio
import json
import math
import os
import signal
import sys
from contextlib import suppress

import structlog

from corp_ed.ingest.extract import (
    ERROR_MESSAGES,
    MAX_EXTRACTED_CHARS,
    ExtractionError,
    SourceFormat,
)

logger = structlog.get_logger()

TIMEOUT_SECONDS = 90.0
MAX_OUTPUT_BYTES = 6 * MAX_EXTRACTED_CHARS + 64 * 1024
"""Потолок ответа дочернего процесса. Честный ответ меньше: в Markdown не
больше MAX_EXTRACTED_CHARS символов, в JSON символ — до 6 байт (`\\u001f`).
Больше пишет только сломанный или захваченный процесс: его убиваем, не
дочитывая, — иначе гигабайты из pipe легли бы в память API (RLIMIT_FSIZE
на pipe не действует). Код ошибки — `document_too_large`."""
_STDERR_TAIL_BYTES = 500
_CHUNK_BYTES = 64 * 1024
# Убит ядром по лимиту CPU (SIGXCPU, при равных soft/hard — сразу
# SIGKILL) — это «слишком долго», а не «файл повреждён»: сотрудник
# увидит честную причину, а не совет перезалить файл.
_KILLED_BY_LIMIT = frozenset({-signal.SIGKILL, -signal.SIGXCPU})


def cpu_budget(timeout: float) -> int:
    """CPU-секунды дочернему процессу: таймаут по стене на число ядер.

    Нативные библиотеки разбора могут занимать несколько ядер, поэтому
    лимит CPU меньше timeout × ядер срабатывал бы на
    честном документе раньше таймаута.
    """
    return max(1, math.ceil(timeout)) * max(1, os.cpu_count() or 1)


def _crash_code(returncode: int) -> str:
    return "timeout" if returncode in _KILLED_BY_LIMIT else "corrupted"


def _clean_env() -> dict[str, str]:
    # Никаких секретов из окружения API: процесс читает враждебный файл.
    return {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"}


def _worker_command(fmt: SourceFormat, cpu_seconds: int) -> list[str]:
    return [
        sys.executable,
        # -I: без PYTHONPATH, пользовательского site и текущего каталога
        # в sys.path — дочерний процесс грузит только установленный пакет.
        "-I",
        "-m",
        "corp_ed.ingest.extract_worker",
        fmt.value,
        str(cpu_seconds),
    ]


async def _feed(stdin: asyncio.StreamWriter, data: bytes) -> None:
    try:
        stdin.write(data)
        await stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        # Дочерний процесс не дочитал файл (упал или убит): причину
        # скажет код выхода.
        pass
    finally:
        stdin.close()


async def _read_output(
    process: asyncio.subprocess.Process, stdout: asyncio.StreamReader
) -> bytes | None:
    """Ответ целиком; больше MAX_OUTPUT_BYTES — процесс убит, None."""
    output = bytearray()
    while chunk := await stdout.read(_CHUNK_BYTES):
        output += chunk
        if len(output) > MAX_OUTPUT_BYTES:
            with suppress(ProcessLookupError):
                process.kill()
            return None
    return bytes(output)


async def _read_tail(stderr: asyncio.StreamReader) -> bytes:
    """stderr до конца, в памяти — только хвост для журнала."""
    tail = b""
    while chunk := await stderr.read(_CHUNK_BYTES):
        tail = (tail + chunk)[-_STDERR_TAIL_BYTES:]
    return tail


async def _communicate(
    process: asyncio.subprocess.Process, data: bytes
) -> tuple[bytes | None, bytes]:
    """Как Process.communicate, но stdout — с потолком, а от stderr
    остаётся только хвост: память API не зависит от того, что пишет
    дочерний процесс."""
    stdin, stdout, stderr = process.stdin, process.stdout, process.stderr
    if stdin is None or stdout is None or stderr is None:
        raise RuntimeError("extract worker started without pipes")
    async with asyncio.TaskGroup() as group:
        group.create_task(_feed(stdin, data))
        output = group.create_task(_read_output(process, stdout))
        errors = group.create_task(_read_tail(stderr))
    await process.wait()
    return output.result(), errors.result()


async def extract_isolated(
    fmt: SourceFormat,
    data: bytes,
    *,
    timeout: float = TIMEOUT_SECONDS,
    cpu_seconds: int | None = None,
) -> str:
    process = await asyncio.create_subprocess_exec(
        *_worker_command(fmt, cpu_seconds or cpu_budget(timeout)),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_clean_env(),
    )
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await _communicate(process, data)
    except TimeoutError:
        process.kill()
        await process.wait()
        logger.warning("extraction_timeout", format=fmt.value, size=len(data))
        raise ExtractionError("timeout") from None

    if stdout is None:
        # Честный ответ столько не весит (MAX_OUTPUT_BYTES): процесс уже
        # убит, ответ не читается.
        logger.warning(
            "extraction_output_too_large",
            format=fmt.value,
            size=len(data),
            limit=MAX_OUTPUT_BYTES,
        )
        raise ExtractionError("document_too_large")

    if process.returncode != 0:
        # Убит по лимиту CPU/памяти или упал в C-коде парсера.
        code = _crash_code(process.returncode or 0)
        logger.warning(
            "extraction_crashed",
            format=fmt.value,
            size=len(data),
            returncode=process.returncode,
            code=code,
            stderr=stderr.decode("utf-8", "replace"),
        )
        raise ExtractionError(code)

    try:
        result = json.loads(stdout)
    except ValueError:
        raise ExtractionError("corrupted") from None

    if result.get("ok") is True and isinstance(result.get("markdown"), str):
        markdown: str = result["markdown"]
        return markdown

    code = result.get("code")
    # Код из дочернего процесса — тоже вход извне: только известные.
    raise ExtractionError(code if code in ERROR_MESSAGES else "corrupted")
