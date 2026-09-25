"""Проба модели эмбеддингов: лимит входа, размерность, режимы, квота (A1, задача 1.1).

    python -m eval.probe_embedding_limit [--text длинный_документ.md]
    python -m eval.probe_embedding_limit --model text-embeddings-v2 \\
        --text doc.md --sample chunks.txt --name embeddings_v2_probe

Вопросы из ТЗ:
1. Какой лимит входа у text-search-doc в токенах?
2. Что при превышении: ошибка или молчаливая обрезка? У «Честного знака»
   эмбеддер с окном 512 токенов молча обрезал чанки в 4096 символов, и
   качество падало незаметно.
3. Сколько символов кириллицы на токен у модели эмбеддингов (count_tokens
   сейчас считает 3, замер был на YandexGPT)?

Как:
- шлём префиксы одного текста растущей длины и записываем код ответа,
  numTokens и текст ошибки;
- если длинный префикс вернул ошибку, двоичным поиском находим самую
  длинную длину, которую API принимает, и её numTokens — это лимит;
- обрезку ловим так: если эмбеддинг префикса длины L совпадает
  (косинус > 0.99999) с эмбеддингом всего текста, то всё после L модель
  не видит. Двоичным поиском находим, с какой длины эмбеддинг перестаёт
  меняться, — это и есть реальное окно.

С --model text-embeddings-v2 (25.09) ещё:
4. Размерность: у v2 она задаётся полем dim (128, 256, 512, 768 по
   документации) — проверяем, что приходит и что бывает с другими.
5. Режимы документ / запрос: одинаковый текст в -doc и -query — разные
   ли векторы (разные ли это модели).
6. Квота: пачка запросов без ограничителя — с какой частоты 429.
7. Символы на токен на реальных чанках (--sample: по тексту на строку).

Отчёт печатается и сохраняется в eval/results/<дата>_<name>.md.
Стоимость — сотни коротких запросов эмбеддинга, доли рубля.
"""

import argparse
import json
import math
import re
import statistics
import sys
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

LENGTHS = (250, 500, 1000, 2000, 4000, 8000, 16000, 32000, 64000)
SAME_VECTOR = 0.99999
SEARCH_PRECISION_CHARS = 50


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def find_cutoff(
    embed_prefix: Callable[[int], Sequence[float]],
    total_chars: int,
    precision: int = SEARCH_PRECISION_CHARS,
) -> int | None:
    """Наименьшая длина префикса, после которой эмбеддинг не меняется.

    None — обрезки нет: эмбеддинг префикса короче текста отличается от
    эмбеддинга всего текста.
    """
    full = embed_prefix(total_chars)
    low, high = 1, total_chars
    if cosine(embed_prefix(max(1, total_chars - precision)), full) < SAME_VECTOR:
        return None
    while high - low > precision:
        middle = (low + high) // 2
        if cosine(embed_prefix(middle), full) >= SAME_VECTOR:
            high = middle
        else:
            low = middle
    return high


def max_accepted(
    accepts: Callable[[int], bool], low: int, high: int, precision: int = 40
) -> int:
    """Самая длинная длина префикса, которую API принимает (low — да, high — нет)."""
    while high - low > precision:
        middle = (low + high) // 2
        if accepts(middle):
            low = middle
        else:
            high = middle
    return low


_SESSION_PREFIX = re.compile(r"^Error in session [^:]*:\s*")


def error_message(body: str) -> str:
    """Суть ошибки AI Studio без служебного префикса с id сессии.

    «Error in session internal_id=…&request_id=…: number of input tokens
    must be no more than 2048, got 2908» → «number of input tokens …».
    """
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body.strip()[:200]
    text = (
        str(data.get("message") or data.get("error") or body)
        if isinstance(data, dict)
        else body
    )
    return _SESSION_PREFIX.sub("", text).strip()[:200]


def default_text() -> str:
    """Длинный непериодичный русский текст, если свой не передан."""
    parts = []
    for i in range(1, 400):
        parts.append(
            f"Пункт {i}. Работник подразделения номер {i * 7 % 97} вправе обратиться "
            f"в отдел кадров с заявлением по вопросу {i * 13 % 89}, а срок "
            f"рассмотрения составляет {i % 30 + 1} рабочих дней с даты регистрации."
        )
    return " ".join(parts)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.probe_embedding_limit")
    parser.add_argument(
        "--text", type=Path, help="длинный документ (≥ 32 000 символов)"
    )
    parser.add_argument("--out", type=Path, default=Path("eval/results"))
    parser.add_argument("--model", default="text-search", help="семейство эмбеддингов")
    parser.add_argument(
        "--sample", type=Path, help="реальные чанки, по одному на строку"
    )
    parser.add_argument("--name", default="embedding_probe", help="имя файла отчёта")
    args = parser.parse_args(argv)

    from eval.yandex import YandexClient, YandexError

    client = YandexClient.from_env(embedding_model=args.model)
    text = args.text.read_text(encoding="utf-8") if args.text else default_text()
    text = " ".join(text.split())
    source = args.text.name if args.text else "встроенный текст"
    lines = [
        f"# Проба {args.model}-doc — {date.today().isoformat()}",
        "",
        f"Текст: {source}, {len(text)} символов.",
        "",
    ]
    lines += [
        "| Символов | Результат | numTokens | Символов на токен |",
        "|---|---|---|---|",
    ]

    ratios: list[float] = []
    max_ok = 0
    first_error = 0
    for length in (n for n in LENGTHS if n <= len(text)):
        try:
            embedding = client.embed(text[:length], "doc")
        except YandexError as error:
            first_error = first_error or length
            lines.append(
                f"| {length} | ошибка {error.status}: {error_message(error.body)} "
                "| — | — |"
            )
            continue
        ratio = length / embedding.num_tokens if embedding.num_tokens else float("nan")
        ratios.append(ratio)
        max_ok = length
        lines.append(f"| {length} | ok | {embedding.num_tokens} | {ratio:.2f} |")

    lines.append("")
    if max_ok and first_error > max_ok:

        def accepts(n: int) -> bool:
            try:
                client.embed(text[:n], "doc")
            except YandexError:
                return False
            return True

        limit_chars = max_accepted(accepts, max_ok, first_error)
        limit_tokens = client.embed(text[:limit_chars], "doc").num_tokens
        lines.append(
            f"**Лимит входа:** принимается до ~{limit_chars} символов = "
            f"{limit_tokens} токенов; длиннее — ошибка API, а не обрезка."
        )
    if ratios:
        lines.append(
            f"Медиана символов на токен: **{statistics.median(ratios):.2f}** "
            f"(count_tokens сейчас считает 3.0)."
        )
    if max_ok:
        cache: dict[int, Sequence[float]] = {}

        def embed_prefix(n: int) -> Sequence[float]:
            if n not in cache:
                cache[n] = client.embed(text[:n], "doc").vector
            return cache[n]

        cutoff = find_cutoff(embed_prefix, max_ok)
        if cutoff is None:
            lines.append(f"Молчаливой обрезки до {max_ok} символов не найдено.")
        else:
            tokens = client.embed(text[:cutoff], "doc").num_tokens
            lines.append(
                f"**Молчаливая обрезка**: после ~{cutoff} символов (≈{tokens} токенов "
                f"по numTokens) эмбеддинг не меняется — дальше модель текст не видит."
            )
            lines.append(
                "Чанк вместе с крошками должен быть меньше этого окна; при 400 "
                "токенах тела и крошках до ~60 токенов запас нужен ≥ 460 токенов."
            )

    if args.model != "text-search":
        lines += extra_checks(args.model, args.sample)

    report = "\n".join(lines)
    print(report)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{date.today().isoformat()}_{args.name}.md"
    path.write_text(report + "\n", encoding="utf-8")
    print(f"\nОтчёт: {path}")
    return 0


DIMS = (None, 128, 256, 512, 768, 1024)
BURST = 40


def extra_checks(model: str, sample: Path | None) -> list[str]:
    """Размерность, режимы документ/запрос, квота, символы на токен."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    from eval.yandex import YandexClient, YandexError

    probe = "Срок выполнения работ по проекту составляет 12 месяцев с даты договора."
    lines = [
        "",
        "## Размерность (поле dim)",
        "",
        "| dim | Результат | Длина вектора | Норма |",
        "|---|---|---|---|",
    ]
    for dim in DIMS:
        client = YandexClient.from_env(embedding_model=model, embedding_dim=dim)
        try:
            vector = client.embed(probe, "doc").vector
        except YandexError as error:
            lines.append(
                f"| {dim} | ошибка {error.status}: {error_message(error.body)} "
                "| — | — |"
            )
            continue
        norm = math.sqrt(sum(x * x for x in vector))
        lines.append(f"| {dim or 'не задан'} | ok | {len(vector)} | {norm:.4f} |")

    client = YandexClient.from_env(embedding_model=model)
    doc = client.embed(probe, "doc").vector
    query = client.embed(probe, "query").vector
    lines += [
        "",
        "## Режимы документ / запрос",
        "",
        f"Один и тот же текст в `{model}-doc` и `{model}-query`: косинус "
        f"{cosine(doc, query):.4f} (1.0 — одна модель, меньше — разные).",
    ]

    raw = YandexClient.from_env(
        embedding_model=model, embedding_rps=1000.0, max_attempts=1
    )
    statuses: list[int] = []

    def one(i: int) -> None:
        try:
            raw.embed(f"{probe} Вариант {i}.", "query")
            statuses.append(200)
        except YandexError as error:
            statuses.append(error.status)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(one, range(BURST)))
    elapsed = time.perf_counter() - started
    lines += [
        "",
        "## Квота",
        "",
        f"{BURST} запросов в 20 потоков без ограничителя за {elapsed:.1f} с "
        f"(~{BURST / elapsed:.0f} в секунду): успешно {statuses.count(200)}, "
        f"429 — {statuses.count(429)}, другие ошибки — "
        f"{len(statuses) - statuses.count(200) - statuses.count(429)}.",
    ]

    if sample:
        texts = [
            t for t in sample.read_text(encoding="utf-8").splitlines() if t.strip()
        ]
        v1 = YandexClient.from_env(embedding_model="text-search")
        ratios: list[float] = []
        v1_ratios: list[float] = []
        for text in texts:
            n = client.embed(text, "doc").num_tokens
            n1 = v1.embed(text, "doc").num_tokens
            if n and n1:
                ratios.append(len(text) / n)
                v1_ratios.append(len(text) / n1)
        lines += [
            "",
            "## Символы на токен на реальных чанках",
            "",
            f"{len(ratios)} чанков: медиана {statistics.median(ratios):.2f} символа на "
            f"токен у {model}, {statistics.median(v1_ratios):.2f} у text-search "
            "(count_tokens считает 3.0).",
        ]
    return lines


if __name__ == "__main__":
    sys.exit(main())
