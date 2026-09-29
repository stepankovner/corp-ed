"""Прогон eval через HTTP API (A6).

    python -m eval.run_eval retrieval --dataset eval/private/golden.csv --config v2
    python -m eval.run_eval e2e       --dataset eval/private/golden.csv --config lite-k5
    python -m eval.run_eval score     --results eval/results/2026-10-02_lite-k5_e2e.csv

retrieval — POST /faq/search: Hit Rate@1/3/5/10, MRR с 95% CI, «воронка»,
    расстояние до ближайшего чанка по каждому вопросу (для порога, A8).
e2e — POST /faq/ask: классификация «ответил / отказал» (precision,
    recall, F1), латентность p50/p95. В CSV — пустые колонки correct
    (0/1/2), faithful (0/1), comment для ручной разметки.
score — после ручной разметки: правильность по типам вопросов и
    проверка планки качества из mvp-plan (допущение).

Результаты: eval/results/<дата>_<конфиг>_<режим>.csv + строка
в eval/results/summary.csv. --config — это имя конфигурации бэкенда
(параметры нарезки, faq_limit, модель): сам скрипт их не меняет.
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from corp_ed.prompts.faq import is_not_found
from eval.api_client import CorpEdClient
from eval.datasets import EvalItem, load_dataset, select_split
from eval.metrics import percentile, refusal_prf
from eval.relevance import RetrievedChunk
from eval.results import RESULTS_DIR, append_summary, read_csv, results_path, write_csv
from eval.retrieval_eval import evaluate_retrieval, format_report

_CITATION = re.compile(r"\[\d+\]")
_REFUSAL_PARAPHRASE = re.compile(
    r"(?:нет|не\s+(?:содерж|найд|указ|упомина|привод|описан|сказано)|отсутству)"
    r".{0,60}(?:выдерж|документ|материал|фрагмент)"
    r"|(?:выдерж|документ|материал|фрагмент).{0,60}"
    r"(?:нет\b|не\s+(?:содерж|найд|указ|упомина|привод|описан|сказано)|отсутству)",
    re.IGNORECASE | re.DOTALL,
)

QUALITY_BAR_CORRECT = 0.8
"""Допущение mvp-plan: 20 вопросов по корпусу — ≥ 16 правильных (80%)."""
QUALITY_BAR_FALSE_ANSWERS = 0.2
"""Допущение mvp-plan: 5 вопросов вне корпуса — ≤ 1 ложного ответа (20%)."""


def looks_like_refusal(answer: str) -> bool:
    """Отказ своими словами вместо фиксированной фразы.

    «В предоставленных выдержках нет информации…» — по смыслу отказ, но
    фронт его не распознает. Для классификации считаем отказом, а
    отдельно считаем как нарушение формата: частота таких ответов —
    показатель того, как модель держит инструкции (E5: lite против Pro).
    Ответ со ссылками [n] отказом не считается: это частичный ответ.
    """
    if is_not_found(answer) or _CITATION.search(answer):
        return False
    first_sentence = re.split(r"(?<=[.!?])\s", answer.strip(), maxsplit=1)[0]
    return bool(_REFUSAL_PARAPHRASE.search(first_sentence))


def is_answered(answer: str, answer_given: bool) -> bool:
    return answer_given and not is_not_found(answer) and not looks_like_refusal(answer)


# --- retrieval ---------------------------------------------------------------------


def run_retrieval(
    client: CorpEdClient, items: Sequence[EvalItem], limit: int
) -> tuple[dict[str, list[RetrievedChunk]], dict[str, float]]:
    retrieved: dict[str, list[RetrievedChunk]] = {}
    latencies: dict[str, float] = {}
    for number, item in enumerate(items, start=1):
        chunks, latency = client.search(item.question, limit)
        retrieved[item.id] = chunks
        latencies[item.id] = latency
        print(
            f"[{number}/{len(items)}] {item.id}: {len(chunks)} чанков, {latency:.0f} мс"
        )
    return retrieved, latencies


# --- e2e -----------------------------------------------------------------------------


def run_e2e(client: CorpEdClient, items: Sequence[EvalItem]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for number, item in enumerate(items, start=1):
        result = client.ask(item.question)
        answered = is_answered(result.content, result.answer_given)
        rows.append(
            {
                "id": item.id,
                "type": item.type,
                "in_corpus": item.in_corpus,
                "question": item.question,
                "expected_answer": item.expected_answer,
                "expected_material": item.expected_material,
                "answer": result.content,
                "answered": answered,
                "answer_given": result.answer_given,
                "refusal_paraphrase": looks_like_refusal(result.content),
                "n_sources": len(result.sources),
                "sources": json.dumps(
                    [
                        {
                            "material": s.material,
                            "heading_path": s.heading_path,
                            "content": s.content,
                            "distance": s.distance,
                        }
                        for s in result.sources
                    ],
                    ensure_ascii=False,
                ),
                "latency_ms": round(result.latency_ms),
                "extra": json.dumps(result.extra, ensure_ascii=False)
                if result.extra
                else "",
                "correct": "",
                "faithful": "",
                "comment": "",
            }
        )
        status = "ответил" if answered else "отказал"
        print(
            f"[{number}/{len(items)}] {item.id}: {status}, {result.latency_ms:.0f} мс"
        )
    return rows


def summarize_e2e(rows: Sequence[dict[str, object]]) -> dict[str, object]:
    answered = [bool(row["answered"]) for row in rows]
    in_corpus = [bool(row["in_corpus"]) for row in rows]
    precision, recall, f1 = refusal_prf(answered, in_corpus)
    latencies = [float(str(row["latency_ms"])) for row in rows]
    return {
        "n": len(rows),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "latency_p50_ms": percentile(latencies, 50),
        "latency_p95_ms": percentile(latencies, 95),
        "refusal_paraphrases": sum(1 for row in rows if row.get("refusal_paraphrase")),
    }


# --- score ---------------------------------------------------------------------------


def _as_bool(value: str) -> bool:
    return value.strip().casefold() in {"true", "1", "yes", "да"}


@dataclass(frozen=True)
class Score:
    correct_rate: float
    in_corpus_correct_rate: float
    false_answer_rate: float
    quality_bar_passed: bool
    unlabeled: list[str]
    by_type: dict[str, float]


def score_rows(rows: Sequence[dict[str, str]]) -> Score:
    """Правильность после ручной разметки колонки correct (0/1/2).

    Вопрос вне корпуса с пустой разметкой оценивается автоматически:
    отказ — 2, ответ — 0. Правильным считается correct = 2 («по
    существу верно»); 1 — частично, в долю правильных не входит.
    """
    by_type: dict[str, list[int]] = defaultdict(list)
    unlabeled: list[str] = []
    correct_in_corpus: list[int] = []
    false_answers = 0
    outside = 0

    for row in rows:
        in_corpus = _as_bool(row["in_corpus"])
        answered = _as_bool(row["answered"])
        label = row.get("correct", "").strip()
        if not label and not in_corpus:
            label = "0" if answered else "2"
        if not label:
            unlabeled.append(row["id"])
            continue
        value = int(label)
        if value not in (0, 1, 2):
            raise ValueError(f"{row['id']}: correct must be 0, 1 or 2, got {label!r}")
        by_type[row["type"]].append(value)
        if in_corpus:
            correct_in_corpus.append(value)
        else:
            outside += 1
            false_answers += int(answered)

    labeled = [value for values in by_type.values() for value in values]
    correct_rate = sum(v == 2 for v in labeled) / len(labeled) if labeled else 0.0
    in_corpus_rate = (
        sum(v == 2 for v in correct_in_corpus) / len(correct_in_corpus)
        if correct_in_corpus
        else 0.0
    )
    false_rate = false_answers / outside if outside else 0.0
    return Score(
        correct_rate=correct_rate,
        in_corpus_correct_rate=in_corpus_rate,
        false_answer_rate=false_rate,
        quality_bar_passed=in_corpus_rate >= QUALITY_BAR_CORRECT
        and false_rate <= QUALITY_BAR_FALSE_ANSWERS,
        unlabeled=unlabeled,
        by_type={
            question_type: sum(v == 2 for v in values) / len(values)
            for question_type, values in sorted(by_type.items())
        },
    )


# --- CLI -----------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.run_eval", description=__doc__
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    for mode in ("retrieval", "e2e"):
        command = sub.add_parser(mode)
        command.add_argument("--dataset", type=Path, required=True)
        command.add_argument("--config", required=True, help="имя конфигурации бэкенда")
        command.add_argument("--out", type=Path, default=RESULTS_DIR)
        command.add_argument("--notes", default="")
        command.add_argument(
            "--split", default="", help="только вопросы этого split (dev/test)"
        )
        if mode == "retrieval":
            command.add_argument(
                "--limit", type=int, default=10, help="top-K для /faq/search"
            )

    score = sub.add_parser("score")
    score.add_argument("--results", type=Path, required=True)
    score.add_argument("--out", type=Path, default=RESULTS_DIR)
    return parser


def _select(items: list[EvalItem], split: str) -> list[EvalItem]:
    # Holdout золотого набора — только явно (--split holdout), задача 2.6.
    return select_split(items, split)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    today = date.today().isoformat()

    if args.mode == "score":
        rows = read_csv(args.results)
        scored = score_rows(rows)
        print(f"Правильных (correct=2): {scored.correct_rate:.3f}")
        print(f"  по корпусу: {scored.in_corpus_correct_rate:.3f}")
        print(f"  ложных ответов вне корпуса: {scored.false_answer_rate:.3f}")
        for question_type, rate in scored.by_type.items():
            print(f"  {question_type:<14} {rate:.3f}")
        if scored.unlabeled:
            print(f"Не размечены: {', '.join(scored.unlabeled)}")
        verdict = "ПРОЙДЕНА" if scored.quality_bar_passed else "НЕ ПРОЙДЕНА"
        print(f"Планка качества mvp-plan (допущение): {verdict}")
        summary = summarize_e2e(
            [
                {
                    "answered": _as_bool(r["answered"]),
                    "in_corpus": _as_bool(r["in_corpus"]),
                    "latency_ms": r["latency_ms"],
                }
                for r in rows
            ]
        )
        append_summary(
            args.out,
            {
                **summary,
                "date": today,
                "config": args.results.stem,
                "mode": "e2e-scored",
                "correct_rate": scored.correct_rate,
                "results_file": args.results.name,
            },
        )
        return 0

    items = _select(load_dataset(args.dataset), args.split)
    client = CorpEdClient.from_env()
    path = results_path(args.out, args.config, args.mode)

    if args.mode == "retrieval":
        retrieved, latencies = run_retrieval(client, items, args.limit)
        report = evaluate_retrieval(items, retrieved, latencies)
        write_csv(path, report.rows)
        summary = report.summary
        print(format_report(report))
    else:
        e2e_rows = run_e2e(client, items)
        write_csv(path, e2e_rows)
        summary = summarize_e2e(e2e_rows)
        print(
            f"Отказ: precision={summary['precision']:.3f} "
            f"recall={summary['recall']:.3f} "
            f"F1={summary['f1']:.3f}; "
            f"латентность p50={summary['latency_p50_ms']:.0f} мс "
            f"p95={summary['latency_p95_ms']:.0f} мс; "
            f"отказов своими словами: {summary['refusal_paraphrases']}"
        )
        print(f"Разметь колонку correct (0/1/2) в {path} и запусти режим score.")

    append_summary(
        args.out,
        {
            **summary,
            "date": today,
            "config": args.config,
            "mode": args.mode,
            "dataset": args.dataset.name + (f":{args.split}" if args.split else ""),
            "results_file": path.name,
            "notes": args.notes,
        },
    )
    print(f"Результаты: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
