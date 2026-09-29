"""Серебряный набор (A6): LLM генерирует вопросы к случайным чанкам корпуса.

    python -m eval.generate_silver --corpus corpus/ --n-chunks 120 --out eval/silver.csv

Для каждого из N случайных чанков модель пишет 1–2 вопроса, на которые
этот чанк отвечает, и дословную цитату-доказательство (evidence).

Отличие от ТЗ: эталон — не (material, position), а цитата. Позиции
меняются при каждой перенарезке (E1–E3), и эталон по позиции делал бы
сравнение нарезок бессмысленным; цитата от нарезки не зависит (см.
eval/relevance.py). material и position из генерирующей нарезки
сохраняются для справки.

Фильтры:
- цитата должна найтись в чанке (иначе модель её выдумала);
- цитата не короче MIN_EVIDENCE_WORDS слов: короткая фраза («Заполняется
  при заключении договора») повторяется по всему документу, и Hit
  засчитывается любому чанку с ней — эталон неоднозначен;
- вопрос не должен копировать текст чанка: VERBATIM_NGRAM слов подряд
  из чанка — и вопрос выбрасывается (ТЗ: такие вопросы завышают метрики);
- повторы выбрасываются: тот же вопрос или почти та же цитата из того же
  документа. Одно место документа бывает в двух чанках (перекрытие,
  повторяющиеся формы), и без этого пара вопросов о нём попадала в dev
  и test одновременно (прогон 24.09: 11 пар на 184 вопроса).

Деление на dev/test (как у Битрикс24): параметры подбираются на dev,
подтверждаются на test. Делится по чанку: оба вопроса одного чанка
попадают в одну часть, иначе test «подсматривает» в dev.

Предупреждение Битрикс24: синтетика — грубое сито для параметров. На
сложных живых вопросах разница между конфигурациями бывает больше.
Финальный выбор — по золотому набору.
"""

import argparse
import hashlib
import json
import random
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from corp_ed.domain.markdown import to_plain_text
from corp_ed.domain.tokens import count_tokens
from corp_ed.llm.types import Message, Role
from eval.corpus import BenchChunk, ChunkingConfig, chunk_corpus, load_corpus
from eval.datasets import SILVER_COLUMNS
from eval.relevance import EVIDENCE_MIN_COVERAGE, evidence_coverage, words
from eval.results import write_csv

SILVER_PROMPT_VERSION = "silver-v1"
MIN_CHUNK_TOKENS = 40
MIN_EVIDENCE_WORDS = 5
VERBATIM_NGRAM = 5
TEST_SHARE = 0.3

_SYSTEM = (
    "Ты составляешь проверочные вопросы для поиска по внутренним документам "
    "компании. Отвечай только JSON без пояснений."
)

_TASK = """\
Фрагмент документа «{source}»:
<<<
{text}
>>>

Составь {n} вопрос(а), которые сотрудник компании мог бы задать в чате и на \
которые этот фрагмент отвечает.

Требования:
- вопрос звучит естественно, как пишут в чат: «Сколько…», «Как…», «Можно ли…», \
«Кто…», «Что делать, если…»;
- пиши своими словами: не повторяй подряд больше трёх слов из фрагмента, \
используй синонимы и разговорные формулировки;
- ответ должен однозначно следовать из фрагмента;
- не спрашивай про название документа, номера пунктов и оформление;
- вопросы должны быть о разном;
- для каждого вопроса укажи evidence — ОДНО предложение из фрагмента \
дословно, в котором есть ответ.

Формат ответа:
{{"questions": [{{"question": "...", "evidence": "..."}}]}}"""


@dataclass(frozen=True)
class SilverQuestion:
    question: str
    evidence: str


def build_silver_messages(chunk: BenchChunk, n_questions: int) -> list[Message]:
    body = (
        chunk.llm_text.split("\n", 1)[1] if "\n" in chunk.llm_text else chunk.llm_text
    )
    source = " > ".join([chunk.material, *chunk.heading_path])
    return [
        Message(role=Role.SYSTEM, content=_SYSTEM),
        Message(
            role=Role.USER,
            content=_TASK.format(source=source, text=body.strip(), n=n_questions),
        ),
    ]


def parse_questions(text: str) -> list[SilverQuestion]:
    """Разобрать JSON-ответ модели; терпим к ```json-обёрткам и тексту вокруг."""
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    list_start, list_end = cleaned.find("["), cleaned.rfind("]")
    data: object
    try:
        if start != -1 and end > start and (list_start == -1 or start < list_start):
            data = json.loads(cleaned[start : end + 1])
        elif list_start != -1 and list_end > list_start:
            data = json.loads(cleaned[list_start : list_end + 1])
        else:
            return []
    except json.JSONDecodeError:
        return []

    items = data.get("questions", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    result: list[SilverQuestion] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question", "")).strip()
        evidence = str(item.get("evidence", "")).strip().strip('«»"')
        if question and evidence:
            result.append(SilverQuestion(question=question, evidence=evidence))
    return result


def copies_chunk(question: str, chunk_text: str, n: int = VERBATIM_NGRAM) -> bool:
    """В вопросе есть n слов подряд из текста чанка."""
    q = words(question)
    c = words(chunk_text)
    if len(q) < n:
        return False
    chunk_ngrams = {tuple(c[i : i + n]) for i in range(len(c) - n + 1)}
    return any(tuple(q[i : i + n]) in chunk_ngrams for i in range(len(q) - n + 1))


def assign_split(key: str, test_share: float = TEST_SHARE) -> str:
    """Детерминированно: один и тот же чанк всегда в одной части."""
    bucket = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "test" if bucket < test_share else "dev"


def sample_chunks(chunks: Sequence[BenchChunk], n: int, seed: int) -> list[BenchChunk]:
    """Случайные чанки, достаточно длинные, чтобы по ним можно было спросить."""
    eligible = [
        chunk
        for chunk in chunks
        if count_tokens(to_plain_text(chunk.llm_text.split("\n", 1)[-1]))
        >= MIN_CHUNK_TOKENS
    ]
    rng = random.Random(seed)
    return rng.sample(eligible, min(n, len(eligible)))


@dataclass
class FilterStats:
    generated: int = 0
    bad_evidence: int = 0
    short_evidence: int = 0
    verbatim: int = 0
    duplicate: int = 0
    kept: int = 0


@dataclass
class SeenQuestions:
    """Принятые вопросы: повтор — тот же вопрос или та же цитата документа."""

    questions: set[str] = field(default_factory=set)
    evidence: dict[str, list[str]] = field(default_factory=dict)

    def is_duplicate(self, material: str, candidate: SilverQuestion) -> bool:
        if " ".join(words(candidate.question)) in self.questions:
            return True
        return any(
            evidence_coverage(candidate.evidence, known) >= EVIDENCE_MIN_COVERAGE
            or evidence_coverage(known, candidate.evidence) >= EVIDENCE_MIN_COVERAGE
            for known in self.evidence.get(material, [])
        )

    def add(self, material: str, candidate: SilverQuestion) -> None:
        self.questions.add(" ".join(words(candidate.question)))
        self.evidence.setdefault(material, []).append(candidate.evidence)


def filter_questions(
    chunk: BenchChunk,
    candidates: Sequence[SilverQuestion],
    seen: SeenQuestions,
    stats: FilterStats,
) -> list[SilverQuestion]:
    kept: list[SilverQuestion] = []
    for candidate in candidates:
        stats.generated += 1
        if (
            evidence_coverage(candidate.evidence, chunk.llm_text)
            < EVIDENCE_MIN_COVERAGE
        ):
            stats.bad_evidence += 1
        elif len(words(candidate.evidence)) < MIN_EVIDENCE_WORDS:
            stats.short_evidence += 1
        elif copies_chunk(candidate.question, chunk.llm_text):
            stats.verbatim += 1
        elif seen.is_duplicate(chunk.material, candidate):
            stats.duplicate += 1
        else:
            seen.add(chunk.material, candidate)
            stats.kept += 1
            kept.append(candidate)
    return kept


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.generate_silver",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("eval/silver.csv"))
    parser.add_argument("--n-chunks", type=int, default=120)
    parser.add_argument("--questions-per-chunk", type=int, default=2)
    parser.add_argument("--chunk-tokens", type=int, default=400)
    parser.add_argument("--overlap-tokens", type=int, default=50)
    parser.add_argument("--model", default="yandexgpt-lite")
    parser.add_argument("--temperature", type=float, default=0.6)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    from eval.yandex import YandexClient

    chunking = ChunkingConfig(
        chunk_tokens=args.chunk_tokens, overlap_tokens=args.overlap_tokens
    )
    chunks = chunk_corpus(load_corpus(args.corpus), chunking)
    sampled = sample_chunks(chunks, args.n_chunks, args.seed)
    client = YandexClient.from_env()
    print(f"Чанков: {len(chunks)}, выбрано: {len(sampled)}, модель: {args.model}")

    rows: list[dict[str, object]] = []
    seen = SeenQuestions()
    stats = FilterStats()
    tokens_in = tokens_out = 0

    for number, chunk in enumerate(sampled, start=1):
        completion = client.complete(
            build_silver_messages(chunk, args.questions_per_chunk),
            model=args.model,
            temperature=args.temperature,
            max_tokens=600,
        )
        tokens_in += completion.input_tokens
        tokens_out += completion.output_tokens
        for question in filter_questions(
            chunk, parse_questions(completion.text), seen, stats
        ):
            rows.append(
                {
                    "id": f"s{len(rows) + 1:04d}",
                    "question": question.question,
                    "material": chunk.material,
                    "position": chunk.position,
                    "heading_path": " > ".join(chunk.heading_path),
                    "evidence": question.evidence,
                    "split": assign_split(chunk.id),
                    "chunk_config": chunking.name,
                }
            )
        print(f"[{number}/{len(sampled)}] {chunk.id}: всего вопросов {len(rows)}")

    write_csv(
        args.out, [{column: row[column] for column in SILVER_COLUMNS} for row in rows]
    )
    dev = sum(1 for row in rows if row["split"] == "dev")
    print(
        f"\nСгенерировано {stats.generated}, оставлено {stats.kept} "
        f"(dev {dev}, test {len(rows) - dev}). Выброшено: цитата не найдена — "
        f"{stats.bad_evidence}, короткая цитата — {stats.short_evidence}, "
        f"копия текста — {stats.verbatim}, повтор — {stats.duplicate}."
    )
    print(
        f"Токены: вход {tokens_in}, выход {tokens_out}. Промпт {SILVER_PROMPT_VERSION}."
    )
    print(
        f"Набор: {args.out}. Просмотри 20 случайных вопросов глазами "
        "перед экспериментами."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
