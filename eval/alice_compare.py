"""Сравнение с Алисой AI для бизнеса (задача ML 4): шаблон, слепые файлы, отчёт.

    python -m eval.alice_compare template --dataset demo.csv \\
        --out eval/private/alice_answers.csv
    python -m eval.alice_compare blind --ours <наш_e2e.csv> [ещё …] \\
        --alice eval/private/alice_answers.csv --out-dir eval/private/results/alice
    python -m eval.judge --results <дата>_ours-blind_e2e.csv   # и alice-blind
    python -m eval.alice_compare report --ours <…ours-blind_e2e_judged.csv> \\
        --alice <…alice-blind_e2e_judged.csv> [--exclude g21,g28] [--md отчёт.md]

Публичного API у Алисы AI для бизнеса нет — ответы собирает человек по
шаблону (docs/ml-alice-protocol.md). Здесь:
- template — CSV «вопрос → ответ → ссылка → время» для ручного заполнения;
- normalize_answer — нормализация ответа перед слепой оценкой (4.3):
  без самоназваний, оформления и ссылок, с пометкой «ссылка: есть / нет»;
- blind — два файла в формате результатов e2e на одних и тех же вопросах
  (только те, где ответили обе системы): ответы нормализованы, колонка
  sources пустая у обеих — судья `eval.judge` не видит ни выдержек, ни
  чей это ответ, и оценивает обе системы одним промптом;
- report — метрики протокола по двум файлам после судьи: верные ответы по
  корпусу, ложные ответы вне корпуса, F1 отказа, ссылки, время; разница
  «мы − Алиса» с 95 % CI (бутстреп) и p парного перестановочного теста.
"""

import argparse
import csv
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from corp_ed.prompts.faq import is_not_found
from eval.datasets import load_dataset
from eval.metrics import (
    bootstrap_ci,
    paired_permutation_test,
    percentile,
    refusal_prf,
)
from eval.results import read_csv, write_csv
from eval.run_eval import looks_like_refusal

TEMPLATE_COLUMNS = (
    "id",
    "question",
    "in_corpus",
    "answer",
    "source",
    "seconds",
    "asked_at",
    "notes",
)

NO_SOURCE = ("", "нет", "-", "—")
"""Так человек отмечает в колонке source, что Алиса ничего не показала."""

_SELF_NAMES = re.compile(
    r"^\s*(?:я\s*[—-]\s*)?(?:алиса(?:\s+ai)?|ассистент(?:\s+компании)?)\s*[,.:!—-]\s*",
    re.IGNORECASE,
)
_MARKDOWN = re.compile(r"[*_`#>]+")
_EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿]")
_OUR_CITATION = re.compile(r"\s*\[\d+(?:\.\d+)*\]")
_CITATION_NUMBER = re.compile(r"\[(\d+)(?:\.\d+)*\]")
_LINK = re.compile(r"\[([^\]]+)\]\((?:https?://)?[^)]+\)|https?://\S+")
_SOURCE_LINE = re.compile(
    r"^\s*(?:источник|источники|ссылки?)\s*:.*$", re.IGNORECASE | re.M
)
_ALICE_REFUSAL = re.compile(
    r"^\s*(?:(?:я\s+)?поискал[аи]?\s+в\s+доступных\s+источниках"
    r"|в\s+доступных\s+(?:мне\s+)?источниках\s+нет"
    r"|не\s+получилось\s+найти\s+ответ"
    r"|(?:к\s+сожалению,?\s+)?(?:у\s+меня\s+)?нет\s+(?:достаточной\s+)?информации"
    r"|(?:к\s+сожалению,?\s+)?(?:я\s+)?не\s+(?:нашл[аи]|смогл[аи]\s+найти))",
    re.IGNORECASE,
)


def normalize_answer(answer: str) -> tuple[str, bool]:
    """(ответ без оформления и следов системы, была ли ссылка на источник)."""
    has_link = bool(
        _OUR_CITATION.search(answer)
        or _LINK.search(answer)
        or _SOURCE_LINE.search(answer)
    )
    text = _SOURCE_LINE.sub("", answer)
    text = _LINK.sub(lambda m: m.group(1) or "", text)
    text = _OUR_CITATION.sub("", text)
    text = _EMOJI.sub("", text)
    text = _MARKDOWN.sub("", text)
    text = _SELF_NAMES.sub("", text)
    text = " ".join(text.split())
    return text, has_link


def is_refusal(answer: str) -> bool:
    """Отказ любой из систем — по началу ответа, одинаково для обеих.

    Наш: фиксированная фраза (`is_not_found`; общий ответ с пометкой
    начинается с неё же) или пересказ отказа (`looks_like_refusal`).
    Алисин: «Я поискала в доступных источниках, но не нашла…», «В доступных
    мне источниках нет…», «Не получилось найти ответ». Что идёт после
    «однако…», решает судья: правило 3 judge-v1 — вне корпуса верен только
    отказ, ответ по существу вместо отказа — 0.
    """
    if is_not_found(answer) or looks_like_refusal(answer):
        return True
    return bool(_ALICE_REFUSAL.search(answer))


def doc_key(name: str) -> str:
    """Ключ названия документа: буквы и цифры без регистра и «ё»."""
    return re.sub(r"[^0-9a-zа-я]+", "", name.casefold().replace("ё", "е"))


def doc_matches(cited: str, expected: str) -> bool:
    """Ссылка ведёт на ожидаемый документ: одно название — начало другого.

    «Положение СТАРТ-ИИ-1» (как показывает Алиса) ↔ «Положение Старт-ИИ-1
    (очередь 2)_на сайт» (название материала у нас).
    """
    a, b = doc_key(cited), doc_key(expected)
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


def split_sources(source: str) -> list[str]:
    """«Положение СТАРТ-ИИ-1; UMNIK-2026» → два названия; «нет» → []."""
    if source.strip().casefold() in NO_SOURCE:
        return []
    return [part.strip() for part in re.split(r"[;\n]+", source) if part.strip()]


def cited_materials(answer: str, sources_json: str) -> tuple[bool, list[str]]:
    """(есть ли ссылки [n] в нашем ответе, документы, на которые они ведут)."""
    try:
        sources = json.loads(sources_json) if sources_json else []
    except json.JSONDecodeError:
        sources = []
    numbers = [int(n) for n in _CITATION_NUMBER.findall(answer)]
    materials: list[str] = []
    for number in numbers:
        if 1 <= number <= len(sources):
            material = str(sources[number - 1].get("material", ""))
            if material and material not in materials:
                materials.append(material)
    return bool(numbers), materials


def _flag(value: object) -> bool:
    return str(value).strip().casefold() == "true"


def _blind_row(
    base: Mapping[str, str],
    *,
    raw: str,
    has_link: bool,
    cited: Sequence[str],
    latency_s: str,
    notes: str,
) -> dict[str, object]:
    expected_material = base.get("expected_material", "")
    normalized, _ = normalize_answer(raw)
    source_ok = bool(cited) and all(doc_matches(c, expected_material) for c in cited)
    return {
        "id": base["id"],
        "type": base.get("type", ""),
        "in_corpus": base.get("in_corpus", ""),
        "question": base["question"],
        "expected_answer": base.get("expected_answer", ""),
        "expected_material": expected_material,
        "answer": normalized,
        "answer_raw": raw,
        "refusal": is_refusal(raw),
        "has_link": has_link,
        "cited_docs": "; ".join(cited),
        "source_ok": source_ok,
        "latency_s": latency_s,
        "sources": "",
        "notes": notes,
    }


def build_blind(
    ours: Sequence[Mapping[str, str]], alice: Sequence[Mapping[str, str]]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Пары «наш ответ — ответ Алисы» на общих вопросах, в порядке наших прогонов.

    Эталон, документ и тип берём из нашего файла результатов (тот же
    набор). Ссылка у нас — [n] в тексте и документ выдержки n; у Алисы —
    блок источников интерфейса, записанный человеком в колонку source.
    """
    alice_by_id = {row["id"]: row for row in alice if row.get("answer", "").strip()}
    ours_rows: list[dict[str, object]] = []
    alice_rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in ours:
        row_id = row["id"]
        if row_id in seen or row_id not in alice_by_id or not row.get("answer"):
            continue
        seen.add(row_id)
        theirs = alice_by_id[row_id]
        has_link, cited = cited_materials(row["answer"], row.get("sources", ""))
        latency = row.get("latency_ms", "")
        ours_rows.append(
            _blind_row(
                row,
                raw=row["answer"],
                has_link=has_link,
                cited=cited,
                latency_s=f"{float(latency) / 1000:.2f}" if latency else "",
                notes="",
            )
        )
        their_sources = split_sources(theirs.get("source", ""))
        alice_rows.append(
            _blind_row(
                row,
                raw=theirs["answer"],
                has_link=bool(their_sources),
                cited=their_sources,
                latency_s=theirs.get("seconds", "").strip(),
                notes=theirs.get("notes", ""),
            )
        )
    return ours_rows, alice_rows


@dataclass(frozen=True)
class SystemStats:
    n_in: int
    n_out: int
    correct: int
    partial: int
    wrong: int
    unjudged: int
    correct_general: int
    score: float
    precision: float
    recall: float
    f1: float
    false_out: int
    judge_false_out: int
    answered_in: int
    linked: int
    link_ok: int
    latency_p50: float
    latency_p95: float


def _judge(row: Mapping[str, str]) -> int | None:
    value = row.get("judge_correct", "")
    return int(value) if value != "" else None


def system_stats(rows: Sequence[Mapping[str, str]]) -> SystemStats:
    in_rows = [r for r in rows if _flag(r["in_corpus"])]
    out_rows = [r for r in rows if not _flag(r["in_corpus"])]
    verdicts = [_judge(r) for r in in_rows]
    judged = [v for v in verdicts if v is not None]
    answered = [not _flag(r["refusal"]) for r in rows]
    in_corpus = [_flag(r["in_corpus"]) for r in rows]
    precision, recall, f1 = refusal_prf(answered, in_corpus)
    answered_in = [r for r in in_rows if not _flag(r["refusal"])]
    latencies = [float(r["latency_s"]) for r in rows if r.get("latency_s", "")]
    return SystemStats(
        n_in=len(in_rows),
        n_out=len(out_rows),
        correct=judged.count(2),
        partial=judged.count(1),
        wrong=judged.count(0),
        unjudged=verdicts.count(None),
        correct_general=sum(
            1 for r in in_rows if _flag(r["refusal"]) and _judge(r) == 2
        ),
        score=sum(judged) / len(judged) if judged else 0.0,
        precision=precision,
        recall=recall,
        f1=f1,
        false_out=sum(1 for r in out_rows if not _flag(r["refusal"])),
        judge_false_out=sum(1 for r in out_rows if _judge(r) == 0),
        answered_in=len(answered_in),
        linked=sum(1 for r in answered_in if _flag(r["has_link"])),
        link_ok=sum(1 for r in answered_in if _flag(r["source_ok"])),
        latency_p50=percentile(latencies, 50),
        latency_p95=percentile(latencies, 95),
    )


LOWER_IS_BETTER = frozenset({"false_out", "judge_false_out", "latency"})
"""Метрики, где меньше — лучше (ложные ответы, время)."""


@dataclass(frozen=True)
class Difference:
    metric: str
    n: int
    ours: float
    alice: float
    delta: float
    low: float
    high: float
    p_value: float
    better: int
    worse: int

    @property
    def verdict(self) -> str:
        if self.n == 0:
            return "нет пар"
        better_low = self.metric not in LOWER_IS_BETTER
        if self.low > 0 and self.p_value < 0.05:
            return "мы лучше, вне шума" if better_low else "мы хуже, вне шума"
        if self.high < 0 and self.p_value < 0.05:
            return "мы хуже, вне шума" if better_low else "мы лучше, вне шума"
        return "шум"


def _per_question(rows: Sequence[Mapping[str, str]], metric: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for row in rows:
        in_corpus = _flag(row["in_corpus"])
        verdict = _judge(row)
        if metric == "score" and in_corpus and verdict is not None:
            values[row["id"]] = float(verdict)
        elif metric == "correct" and in_corpus and verdict is not None:
            values[row["id"]] = float(verdict == 2)
        elif metric == "false_out" and not in_corpus:
            values[row["id"]] = float(not _flag(row["refusal"]))
        elif metric == "judge_false_out" and not in_corpus and verdict is not None:
            values[row["id"]] = float(verdict == 0)
        elif metric == "link_ok" and in_corpus:
            values[row["id"]] = float(_flag(row["source_ok"]))
        elif metric == "latency" and row.get("latency_s", ""):
            values[row["id"]] = float(row["latency_s"])
    return values


METRICS = (
    ("score", "балл 0–2 по корпусу"),
    ("correct", "доля верных (2) по корпусу"),
    ("false_out", "ответил вне корпуса (эвристика)"),
    ("judge_false_out", "ложный ответ вне корпуса (судья 0)"),
    ("link_ok", "ссылка на нужный документ (по корпусу)"),
    ("latency", "время ответа, с (все вопросы)"),
)


def differences(
    ours: Sequence[Mapping[str, str]], alice: Sequence[Mapping[str, str]]
) -> list[Difference]:
    """Разница «мы − Алиса» по вопросам, где метрика есть у обеих систем."""
    result: list[Difference] = []
    for metric, _ in METRICS:
        a = _per_question(ours, metric)
        b = _per_question(alice, metric)
        ids = [i for i in a if i in b]
        ours_values = [a[i] for i in ids]
        alice_values = [b[i] for i in ids]
        diffs = [x - y for x, y in zip(ours_values, alice_values, strict=True)]
        low, high = bootstrap_ci(diffs) if diffs else (0.0, 0.0)
        p_value = paired_permutation_test(ours_values, alice_values) if diffs else 1.0
        result.append(
            Difference(
                metric=metric,
                n=len(ids),
                ours=sum(ours_values) / len(ids) if ids else 0.0,
                alice=sum(alice_values) / len(ids) if ids else 0.0,
                delta=sum(diffs) / len(diffs) if diffs else 0.0,
                low=low,
                high=high,
                p_value=p_value,
                better=sum(1 for d in diffs if d > 0),
                worse=sum(1 for d in diffs if d < 0),
            )
        )
    return result


def _ratio(part: int, whole: int) -> str:
    return f"{part}/{whole}" if whole else "—"


def format_report(
    ours: Sequence[Mapping[str, str]],
    alice: Sequence[Mapping[str, str]],
    *,
    title: str = "",
) -> str:
    """Таблицы в Markdown: метрики обеих систем и разницы с CI и p."""
    stats = {"мы": system_stats(ours), "Алиса": system_stats(alice)}
    lines: list[str] = []
    if title:
        lines += [f"### {title}", ""]
    lines += [
        "| Метрика | мы | Алиса |",
        "|---|---|---|",
    ]
    ours_s, alice_s = stats["мы"], stats["Алиса"]
    rows = [
        (
            "вопросов по корпусу / вне",
            f"{ours_s.n_in} / {ours_s.n_out}",
            f"{alice_s.n_in} / {alice_s.n_out}",
        ),
        (
            "верно (2) по корпусу",
            _ratio(ours_s.correct, ours_s.n_in),
            _ratio(alice_s.correct, alice_s.n_in),
        ),
        ("частично (1)", str(ours_s.partial), str(alice_s.partial)),
        ("неверно (0)", str(ours_s.wrong), str(alice_s.wrong)),
        ("без вердикта судьи", str(ours_s.unjudged), str(alice_s.unjudged)),
        (
            "из верных — общий ответ после отказа",
            str(ours_s.correct_general),
            str(alice_s.correct_general),
        ),
        ("средний балл 0–2", f"{ours_s.score:.2f}", f"{alice_s.score:.2f}"),
        (
            "отказ: precision / recall / F1",
            f"{ours_s.precision:.3f} / {ours_s.recall:.3f} / {ours_s.f1:.3f}",
            f"{alice_s.precision:.3f} / {alice_s.recall:.3f} / {alice_s.f1:.3f}",
        ),
        (
            "ответил вне корпуса (эвристика)",
            _ratio(ours_s.false_out, ours_s.n_out),
            _ratio(alice_s.false_out, alice_s.n_out),
        ),
        (
            "ложный ответ вне корпуса (судья)",
            _ratio(ours_s.judge_false_out, ours_s.n_out),
            _ratio(alice_s.judge_false_out, alice_s.n_out),
        ),
        (
            "ссылка есть (из ответов по корпусу)",
            _ratio(ours_s.linked, ours_s.answered_in),
            _ratio(alice_s.linked, alice_s.answered_in),
        ),
        (
            "ссылка на нужный документ",
            _ratio(ours_s.link_ok, ours_s.answered_in),
            _ratio(alice_s.link_ok, alice_s.answered_in),
        ),
        (
            "время ответа p50 / p95, с",
            f"{ours_s.latency_p50:.1f} / {ours_s.latency_p95:.1f}",
            f"{alice_s.latency_p50:.1f} / {alice_s.latency_p95:.1f}",
        ),
    ]
    lines += [f"| {name} | {a} | {b} |" for name, a, b in rows]
    lines += [
        "",
        "| Разница «мы − Алиса» | n | мы | Алиса | Δ [95 % CI] | p "
        "| лучше / хуже | вывод |",
        "|---|---|---|---|---|---|---|---|",
    ]
    labels = dict(METRICS)
    for diff in differences(ours, alice):
        interval = f"{diff.delta:+.2f} [{diff.low:+.2f}; {diff.high:+.2f}]"
        lines.append(
            f"| {labels[diff.metric]} | {diff.n} | {diff.ours:.2f} | {diff.alice:.2f} "
            f"| {interval} | {diff.p_value:.3f} "
            f"| {diff.better} / {diff.worse} | {diff.verdict} |"
        )
    return "\n".join(lines) + "\n"


def _exclude(
    rows: Iterable[Mapping[str, str]], ids: set[str]
) -> list[Mapping[str, str]]:
    return [row for row in rows if row["id"] not in ids]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.alice_compare")
    sub = parser.add_subparsers(dest="command", required=True)
    template = sub.add_parser("template", help="CSV для ручного сбора ответов")
    template.add_argument("--dataset", type=Path, action="append", required=True)
    template.add_argument("--out", type=Path, required=True)
    blind = sub.add_parser("blind", help="два файла для слепой оценки судьёй")
    blind.add_argument(
        "--ours",
        type=Path,
        nargs="+",
        required=True,
        help="наши результаты e2e (один или несколько файлов)",
    )
    blind.add_argument(
        "--alice", type=Path, required=True, help="ответы Алисы (формат template)"
    )
    blind.add_argument("--out-dir", type=Path, required=True)
    blind.add_argument("--day", default=date.today().isoformat())
    report = sub.add_parser("report", help="метрики и разницы после судьи")
    report.add_argument("--ours", type=Path, required=True)
    report.add_argument("--alice", type=Path, required=True)
    report.add_argument(
        "--exclude", default="", help="id через запятую — исключить из обеих систем"
    )
    report.add_argument("--title", default="")
    report.add_argument("--md", type=Path, help="куда записать отчёт")
    args = parser.parse_args(argv)

    if args.command == "template":
        if args.out.exists():
            print(f"{args.out} уже есть — не перезаписываю.")
            return 1
        items = [item for path in args.dataset for item in load_dataset(path)]
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=TEMPLATE_COLUMNS)
            writer.writeheader()
            for item in items:
                writer.writerow(
                    {
                        "id": item.id,
                        "question": item.question,
                        "in_corpus": item.in_corpus,
                    }
                )
        print(f"{len(items)} вопросов → {args.out}")
        return 0

    if args.command == "blind":
        ours = [row for path in args.ours for row in read_csv(path)]
        alice = read_csv(args.alice)
        ours_rows, alice_rows = build_blind(ours, alice)
        if not ours_rows:
            print("Общих вопросов с ответами обеих систем нет.")
            return 1
        ours_path = args.out_dir / f"{args.day}_ours-blind_e2e.csv"
        alice_path = args.out_dir / f"{args.day}_alice-blind_e2e.csv"
        write_csv(ours_path, ours_rows)
        write_csv(alice_path, alice_rows)
        in_corpus = sum(1 for row in ours_rows if _flag(row["in_corpus"]))
        print(
            f"Пар: {len(ours_rows)} (по корпусу {in_corpus}, вне "
            f"{len(ours_rows) - in_corpus}) → {ours_path.name}, {alice_path.name}"
        )
        return 0

    excluded = {part.strip() for part in args.exclude.split(",") if part.strip()}
    ours_judged = _exclude(read_csv(args.ours), excluded)
    alice_judged = _exclude(read_csv(args.alice), excluded)
    text = format_report(ours_judged, alice_judged, title=args.title)
    print(text)
    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(text, encoding="utf-8")
        print(f"→ {args.md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
