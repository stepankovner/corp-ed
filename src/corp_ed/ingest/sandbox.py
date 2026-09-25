"""Запуск извлечения текста в отдельном процессе с ограничениями.

Почему не в процессе API:
- парсеры PDF и docx обрабатывают файл, присланный клиентом; MuPDF
  написан на C, и уязвимость в нём — выполнение кода с правами API,
  где в памяти SECRET_KEY, ключ Yandex Cloud и соединения с базой;
- «PDF-бомба» или бесконечный цикл в парсере занимали бы воркер API;
- разбор PDF — секунды CPU; в event loop он остановил бы все запросы.

Дочерний процесс получает пустое окружение (кроме PATH и локали),
урезает себе память, CPU и запись файлов (extract_worker.py) и убивается
по таймауту. Наружу — только Markdown или код ошибки.
"""

import asyncio
import json
import os
import sys

import structlog

from corp_ed.ingest.extract import ERROR_MESSAGES, ExtractionError, SourceFormat

logger = structlog.get_logger()

TIMEOUT_SECONDS = 90.0


def _clean_env() -> dict[str, str]:
    # Никаких секретов из окружения API: процесс читает враждебный файл.
    return {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"}


async def extract_isolated(
    fmt: SourceFormat, data: bytes, *, timeout: float = TIMEOUT_SECONDS
) -> str:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        # -I: без PYTHONPATH, пользовательского site и текущего каталога
        # в sys.path — дочерний процесс грузит только установленный пакет.
        "-I",
        "-m",
        "corp_ed.ingest.extract_worker",
        fmt.value,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_clean_env(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input=data), timeout=timeout
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        logger.warning("extraction_timeout", format=fmt.value, size=len(data))
        raise ExtractionError("timeout") from None

    if process.returncode != 0:
        # Убит по лимиту CPU/памяти или упал в C-коде парсера.
        logger.warning(
            "extraction_crashed",
            format=fmt.value,
            returncode=process.returncode,
            stderr=stderr[-500:].decode("utf-8", "replace"),
        )
        raise ExtractionError("corrupted")

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
