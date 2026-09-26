"""Замер задержки BAAI/bge-reranker-v2-m3 на CPU (M3, шаг 1).

    pip install sentence-transformers   # тянет torch (CPU-сборка ~200 МБ)
    python -m eval.bench_reranker [--corpus corpus/] [--candidates 20] [--runs 10]

ТЗ: своего GPU нет; вариант «bge-reranker на CPU в нашем контейнере» —
только после замера задержки на 20 кандидатах. У Битрикс24 реранкер
занимал ~2 с медианы на их инфраструктуре. Порядок из ТЗ: замер → eval
(MRR, e2e) → решение; выигрыш < 3 п. п. MRR — не внедрять на MVP.

Почему sentence-transformers, а не FlagEmbedding: у МТС FlagEmbedding не
завёлся в их инфраструктуре, пришлось городить костыли. CrossEncoder из
sentence-transformers запускает ту же модель стандартным путём.

Модель скачивается с Hugging Face при первом запуске (~2,3 ГБ).
Замерять на машине, похожей на прод (число ядер!), — на ноутбуке цифры
будут другими. Печатает медиану и p95 на один вызов (N пар вопрос–чанк).
"""

import argparse
import os
import statistics
import sys
import time
from collections.abc import Sequence
from pathlib import Path

DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"
DEFAULT_QUERY = "Можно ли перенести ежегодный отпуск на следующий год?"


def _candidates(corpus: Path | None, n: int) -> list[str]:
    if corpus is not None:
        from eval.corpus import ChunkingConfig, chunk_corpus, load_corpus

        chunks = chunk_corpus(load_corpus(corpus), ChunkingConfig())
        return [chunk.llm_text for chunk in chunks[:n]]
    base = (
        "Работник вправе перенести часть ежегодного оплачиваемого отпуска на "
        "следующий рабочий год по письменному заявлению и с согласия работодателя. "
    )
    return [f"Раздел {i}. " + base * 12 for i in range(n)]  # ≈ 400 токенов


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.bench_reranker")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--candidates", type=int, default=20)
    parser.add_argument(
        "--keep", type=int, default=5, help="сколько оставить (МТС: 20 → 5)"
    )
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--max-length", type=int, default=512)
    args = parser.parse_args(argv)

    try:
        import torch
        from sentence_transformers import CrossEncoder
    except ImportError:
        print("Нужен пакет: pip install sentence-transformers")
        return 2

    print(f"CPU-потоков torch: {torch.get_num_threads()}, ядер: {os.cpu_count()}")
    started = time.perf_counter()
    model = CrossEncoder(args.model, max_length=args.max_length, device="cpu")
    print(f"Модель загружена за {time.perf_counter() - started:.1f} с")

    passages = _candidates(args.corpus, args.candidates)
    pairs = [(DEFAULT_QUERY, passage) for passage in passages]
    model.predict(pairs[:2])  # прогрев

    timings: list[float] = []
    scores: Sequence[float] = []
    for _ in range(args.runs):
        started = time.perf_counter()
        scores = model.predict(pairs, batch_size=len(pairs))
        timings.append((time.perf_counter() - started) * 1000)

    timings.sort()
    p95 = timings[min(len(timings) - 1, round(0.95 * (len(timings) - 1)))]
    print(
        f"{len(pairs)} кандидатов: медиана {statistics.median(timings):.0f} мс, "
        f"p95 {p95:.0f} мс ({args.runs} прогонов)"
    )
    top = sorted(range(len(pairs)), key=lambda i: float(scores[i]), reverse=True)[
        : args.keep
    ]
    print(
        f"Top-{args.keep} индексы: {top}; "
        f"скоры: {[round(float(scores[i]), 3) for i in top]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
