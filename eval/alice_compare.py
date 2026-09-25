"""Сравнение с Алисой AI для бизнеса (задача ML 4): шаблон и нормализация.

    python -m eval.alice_compare template --dataset demo.csv \\
        --out eval/private/alice_answers.csv

Публичного API у Алисы AI для бизнеса нет — ответы собирает человек по
шаблону (docs/ml-alice-protocol.md). Здесь:
- template — CSV «вопрос → ответ → ссылка → время» для ручного заполнения;
- normalize_answer — нормализация ответа перед слепой оценкой (4.3):
  без самоназваний, оформления и ссылок, со пометкой «ссылка: есть / нет».
"""

import argparse
import csv
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from eval.datasets import load_dataset

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

_SELF_NAMES = re.compile(
    r"^\s*(?:я\s*[—-]\s*)?(?:алиса(?:\s+ai)?|ассистент(?:\s+компании)?)\s*[,.:!—-]\s*",
    re.IGNORECASE,
)
_MARKDOWN = re.compile(r"[*_`#>]+")
_EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿]")
_OUR_CITATION = re.compile(r"\s*\[\d+(?:\.\d+)*\]")
_LINK = re.compile(r"\[([^\]]+)\]\((?:https?://)?[^)]+\)|https?://\S+")
_SOURCE_LINE = re.compile(
    r"^\s*(?:источник|источники|ссылки?)\s*:.*$", re.IGNORECASE | re.M
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.alice_compare")
    sub = parser.add_subparsers(dest="command", required=True)
    template = sub.add_parser("template", help="CSV для ручного сбора ответов")
    template.add_argument("--dataset", type=Path, action="append", required=True)
    template.add_argument("--out", type=Path, required=True)
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


if __name__ == "__main__":
    sys.exit(main())
