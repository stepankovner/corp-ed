"""Запись результатов eval: подробный CSV на прогон + строка в summary.csv.

ТЗ (A6): eval/results/<дата>_<конфиг>.csv + одна строка в
eval/results/summary.csv (конфиг → метрики), чтобы сравнение конфигураций
оставалось в истории. В описание PR — строку из summary.csv до и после.
"""

import csv
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

RESULTS_DIR = Path("eval/results")
SUMMARY_FILE = "summary.csv"

SUMMARY_COLUMNS = (
    "date",
    "config",
    "mode",
    "dataset",
    "n",
    "hit@1",
    "hit@3",
    "hit@5",
    "hit@10",
    "mrr",
    "mrr_ci_low",
    "mrr_ci_high",
    "coverage@90%",
    "precision",
    "recall",
    "f1",
    "correct_rate",
    "latency_p50_ms",
    "latency_p95_ms",
    "results_file",
    "notes",
)


def results_path(
    out_dir: Path, config: str, mode: str, day: date | None = None
) -> Path:
    day = day or date.today()
    safe_config = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in config)
    return out_dir / f"{day.isoformat()}_{safe_config}_{mode}.csv"


def write_csv(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns: list[str] = []
    for row in rows:
        columns.extend(key for key in row if key not in columns)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(
            [{key: _cell(value) for key, value in row.items()} for row in rows]
        )


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def append_summary(out_dir: Path, row: Mapping[str, object]) -> Path:
    """Дописать строку в summary.csv (создать с шапкой, если его нет)."""
    path = out_dir / SUMMARY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file, fieldnames=SUMMARY_COLUMNS, extrasaction="ignore", restval=""
        )
        if is_new:
            writer.writeheader()
        writer.writerow({key: _cell(value) for key, value in row.items()})
    return path


def _cell(value: object) -> object:
    if isinstance(value, float):
        return f"{value:.4f}"
    if value is None:
        return ""
    return value
