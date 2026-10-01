"""Память диалога (ML-2, BH-28): замер на наборе диалогов.

Каждый диалог — один-два прошлых вопроса (`history`, через « || ») и
последний вопрос. Прошлые вопросы задаются по очереди тем же путём, что в
продукте, и дают историю `Turn(вопрос, ответ)`. Последний вопрос мерится
тремя способами поиска и двумя ответами:

- **raw** — как без памяти (`RAG_HISTORY_TURNS=0`): поиск и ответ по
  вопросу как есть;
- **concat** — только поиск: прошлый вопрос + последний одной строкой
  (простая альтернатива переписыванию);
- **mem** — продукт с памятью (BH-28): `build_condense_messages` →
  `parse_condensed` → поиск по переписанному вопросу → ответ с историей
  (`build_faq_messages(..., history=…, standalone_question=…)`).

Виды диалогов (`kind`): `followup` — уточнение, понятное только из
диалога; `standalone` — смена темы, вопрос понятен сам, переписывание его
**не должно менять** (урок M6); `followup_out` — уточнение, ответа на
которое в документах нет.

Пишет два файла для `eval.judge`: `…_raw_e2e.csv` и `…_e2e.csv` (с
памятью). В колонке `question` — смысл вопроса (`expected_standalone`):
судье «А у УМНИК?» без диалога не понять. Сравнение —
`eval.compare raw mem --metric judge_correct`.

    python -m eval.dialogue_eval --corpus ../sandbox/corpus \\
        --dataset eval/private/dialogues_dev.csv --split dev --history-turns 3

Ответы на прошлые реплики кэшируются (`eval/.cache/dialogue_turns.json`):
прогон с другим `--history-turns` за первые реплики не платит.
"""

import argparse
import csv
import hashlib
import json
import re
import statistics
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from corp_ed.domain.credits import credits_for
from corp_ed.llm.types import Message
from corp_ed.prompts.dialogue import (
    CONDENSE_PROMPT_VERSION,
    Turn,
    build_condense_messages,
    parse_condensed,
    recent_turns,
)
from corp_ed.prompts.faq import (
    PROMPT_VERSION,
    build_faq_messages,
    build_general_messages,
    ensure_general_prefix,
    is_not_found,
    normalize_citations,
)
from eval.datasets import EvalItem
from eval.offline_e2e import OfflineMatch, relevant_matches
from eval.relevance import RetrievedChunk, is_relevant

HISTORY_SEPARATOR = " || "
KINDS = ("followup", "standalone", "followup_out")
TOKENS_PER_CREDIT = 4000
"""BH-30 (решение Артёма 29.09): 1 кредит = 4 000 токенов."""
CACHE_PATH = Path("eval/.cache/dialogue_turns.json")


@dataclass(frozen=True)
class Dialogue:
    item: EvalItem
    """Последний вопрос: `item.question` — как его задал сотрудник."""
    history: list[str]
    kind: str
    expected_standalone: str


def load_dialogues(path: Path) -> list[Dialogue]:
    with path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    dialogues: list[Dialogue] = []
    for row in rows:
        kind = row["kind"].strip()
        if kind not in KINDS:
            raise ValueError(f"{row['id']}: kind {kind!r}, ожидается {KINDS}")
        history = [q.strip() for q in row["history"].split("||") if q.strip()]
        if not history:
            raise ValueError(f"{row['id']}: пустая история — это не диалог")
        dialogues.append(
            Dialogue(
                item=EvalItem(
                    id=row["id"].strip(),
                    question=row["question"].strip(),
                    in_corpus=row["in_corpus"].strip().casefold() == "true",
                    type=row["type"].strip(),
                    expected_answer=row["expected_answer"].strip(),
                    expected_material=row["expected_material"].strip(),
                    expected_section=row["expected_section"].strip(),
                    evidence=row.get("evidence", "").strip(),
                    split=row.get("split", "").strip(),
                    level=row.get("level", "").strip(),
                ),
                history=history,
                kind=kind,
                expected_standalone=row["expected_standalone"].strip()
                or row["question"].strip(),
            )
        )
    return dialogues


_TRAILING = re.compile(r"[\s?!.…»«\"']+$")


def same_question(a: str, b: str) -> bool:
    """Переписывание не изменило вопрос: регистр, пробелы, кавычки и знак
    в конце не в счёт."""

    def norm(text: str) -> str:
        return _TRAILING.sub("", " ".join(text.replace("ё", "е").split()).casefold())

    return norm(a) == norm(b)


def concat_query(history: Sequence[str], question: str) -> str:
    """Вариант без модели: прошлый вопрос и последний одной строкой."""
    return f"{history[-1]} {question}" if history else question


def found_rank(item: EvalItem, matches: Sequence[OfflineMatch]) -> int:
    """Место первого правильного фрагмента в выдаче (с 1); 0 — не найден."""
    for rank, match in enumerate(matches, start=1):
        chunk = RetrievedChunk(
            id=str(rank),
            material=match.title,
            content=match.content,
            heading_path=match.heading_path,
        )
        if is_relevant(item, chunk):
            return rank
    return 0


# --- Один ход диалога ----------------------------------------------------------


class CompletionLike(Protocol):
    """То, что нужно от ответа модели (`eval.yandex.Completion`)."""

    @property
    def text(self) -> str: ...
    @property
    def input_tokens(self) -> int: ...
    @property
    def output_tokens(self) -> int: ...
    @property
    def latency_ms(self) -> float: ...


Search = Callable[[str], tuple[list[OfflineMatch], float | None]]
"""Вопрос → выдача top-k и лучшее расстояние (`offline_e2e.retrieve`)."""
Ask = Callable[[list[Message]], CompletionLike]
Context = Callable[[list[OfflineMatch]], list[OfflineMatch]]
"""Отбор выдержек в промпт после порога (`select_context` по бюджету)."""


@dataclass
class TurnResult:
    question: str
    standalone: str
    answer: str
    matches: list[OfflineMatch]
    selected: list[OfflineMatch]
    best_distance: float | None
    general: bool = False
    condense_tokens: int = 0
    answer_tokens: int = 0
    latency_ms: float = 0.0
    calls: int = 0

    @property
    def tokens(self) -> int:
        return self.condense_tokens + self.answer_tokens


@dataclass
class TurnSettings:
    max_distance: float
    general_mode: bool = True
    condense_max_tokens: int = 100


def run_turn(
    question: str,
    history: Sequence[Turn],
    *,
    search: Search,
    ask: Ask,
    ask_condense: Ask,
    context: Context,
    settings: TurnSettings,
    use_memory: bool = True,
) -> TurnResult:
    """Один вопрос сотрудника — как в FaqService с BH-28.

    use_memory=False — продукт без памяти: ни переписывания, ни истории.
    """
    standalone, condense_tokens, latency, calls = question, 0, 0.0, 0
    if use_memory and history:
        reply = ask_condense(build_condense_messages(history, question))
        standalone = parse_condensed(reply.text, question)
        condense_tokens = reply.input_tokens + reply.output_tokens
        latency += reply.latency_ms
        calls += 1

    matches, best = search(standalone)
    selected = context(relevant_matches(matches, settings.max_distance))
    answer_tokens, raw = 0, ""
    answer = ""
    if selected:
        messages = (
            build_faq_messages(
                question, selected, history=history, standalone_question=standalone
            )
            if use_memory and history
            else build_faq_messages(question, selected)
        )
        reply = ask(messages)
        raw = reply.text.strip()
        answer = normalize_citations(raw, selected)
        answer_tokens += reply.input_tokens + reply.output_tokens
        latency += reply.latency_ms
        calls += 1
    general = False
    if settings.general_mode and (not selected or is_not_found(raw)):
        reply = ask(build_general_messages(standalone))
        answer = ensure_general_prefix(reply.text.strip())
        answer_tokens += reply.input_tokens + reply.output_tokens
        latency += reply.latency_ms
        calls += 1
        general = True
    return TurnResult(
        question=question,
        standalone=standalone,
        answer=answer,
        matches=matches,
        selected=selected,
        best_distance=best,
        general=general,
        condense_tokens=condense_tokens,
        answer_tokens=answer_tokens,
        latency_ms=latency,
        calls=calls,
    )


# --- Кэш ответов на прошлые реплики ---------------------------------------------


@dataclass
class TurnCache:
    path: Path
    data: dict[str, dict[str, object]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "TurnCache":
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return cls(path=path, data=data)

    @staticmethod
    def key(model: str, history: Sequence[Turn], question: str, extra: str) -> str:
        payload = json.dumps(
            [model, PROMPT_VERSION, CONDENSE_PROMPT_VERSION, extra]
            + [[t.question, t.answer] for t in history]
            + [question],
            ensure_ascii=False,
        )
        return hashlib.sha1(payload.encode("utf-8"), usedforsecurity=False).hexdigest()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8"
        )


# --- Сводка ----------------------------------------------------------------------


def summarize(rows: Sequence[dict[str, object]]) -> list[str]:
    """Строки отчёта: поиск по видам, переписывание, токены и кредиты."""
    lines: list[str] = []
    for kind in KINDS:
        part = [r for r in rows if r["kind"] == kind]
        if not part:
            continue
        lines.append(f"{kind} ({len(part)}):")
        if kind != "followup_out":
            for variant in ("raw", "concat", "mem"):
                found = sum(1 for r in part if int(str(r[f"rank_{variant}"])) > 0)
                lines.append(
                    f"  нужный фрагмент в топ-5, {variant}: {found} / {len(part)}"
                )
        passed = {
            v: sum(1 for r in part if r[f"passed_{v}"] is True) for v in ("raw", "mem")
        }
        lines.append(
            f"  прошли порог: raw {passed['raw']}, mem {passed['mem']}; "
            f"общий ответ: raw {sum(1 for r in part if r['general_raw'] is True)}, "
            f"mem {sum(1 for r in part if r['general_mem'] is True)}"
        )
        if kind == "standalone":
            same = sum(1 for r in part if r["condense_unchanged"] is True)
            lines.append(f"  переписывание не изменило вопрос: {same} / {len(part)}")
    tokens = [int(str(r["tokens_mem"])) for r in rows]
    if tokens:
        credits = [credits_for(t, TOKENS_PER_CREDIT) for t in tokens]
        lines.append(
            f"токенов на последний вопрос с памятью: медиана "
            f"{statistics.median(tokens):.0f}, максимум {max(tokens)}; "
            f"кредитов (1 = {TOKENS_PER_CREDIT}): 1 — {credits.count(1)}, "
            f"2+ — {sum(1 for c in credits if c >= 2)}"
        )
    return lines


# --- Запуск ----------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    from eval.yandex import DEFAULT_API, DEFAULT_LLM, DEFAULT_MAX_DISTANCE

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--split", default="dev")
    parser.add_argument("--history-turns", type=int, default=3)
    parser.add_argument("--model", default=DEFAULT_LLM)
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--max-distance", type=float, default=DEFAULT_MAX_DISTANCE)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--context-tokens", type=int, default=3000)
    parser.add_argument("--max-tokens", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--config", default="")
    parser.add_argument(
        "--out", type=Path, default=Path("eval/private/results/dialogue")
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover — сеть
    from corp_ed.domain.context import select_context
    from eval.corpus import ChunkingConfig, chunk_corpus, load_corpus
    from eval.offline_e2e import retrieve
    from eval.results import results_path, write_csv
    from eval.yandex import (
        DEFAULT_EMBEDDING_MODEL,
        YandexClient,
        default_embedding_dim,
    )

    args = _parser().parse_args(argv)
    dialogues = [
        d
        for d in load_dialogues(args.dataset)
        if not args.split or d.item.split == args.split
    ]
    chunks = chunk_corpus(load_corpus(args.corpus), ChunkingConfig())
    client = YandexClient.from_env()
    cache = TurnCache.load(CACHE_PATH)
    settings = TurnSettings(max_distance=args.max_distance)
    config = args.config or f"dlg-h{args.history_turns}"
    print(
        f"Диалогов: {len(dialogues)}, чанков: {len(chunks)}, "
        f"история: {args.history_turns}"
    )

    def search(query: str) -> tuple[list[OfflineMatch], float | None]:
        return retrieve(
            chunks,
            [query],
            retriever="vector",
            limit=args.limit,
            workers=args.workers,
            embedding_model=DEFAULT_EMBEDDING_MODEL,
            embedding_dim=default_embedding_dim(DEFAULT_EMBEDDING_MODEL),
        )[0]

    def ask(messages: list[Message]) -> CompletionLike:
        return client.complete(
            messages,
            model=args.model,
            temperature=0.0,
            max_tokens=args.max_tokens,
            api=args.api,
        )

    def ask_condense(messages: list[Message]) -> CompletionLike:
        return client.complete(
            messages, model=args.model, temperature=0.0, max_tokens=100, api=args.api
        )

    def context(matches: list[OfflineMatch]) -> list[OfflineMatch]:
        return list(select_context(matches, args.context_tokens))

    def turn(question: str, history: Sequence[Turn], *, memory: bool) -> TurnResult:
        return run_turn(
            question,
            history,
            search=search,
            ask=ask,
            ask_condense=ask_condense,
            context=context,
            settings=settings,
            use_memory=memory,
        )

    rows_raw: list[dict[str, object]] = []
    rows_mem: list[dict[str, object]] = []
    summary_rows: list[dict[str, object]] = []
    for number, dlg in enumerate(dialogues, start=1):
        turns: list[Turn] = []
        for previous in dlg.history:
            visible = recent_turns(turns, args.history_turns)
            key = TurnCache.key(args.model, visible, previous, f"d{args.max_distance}")
            if key not in cache.data:
                result = turn(previous, visible, memory=args.history_turns > 0)
                cache.data[key] = {"answer": result.answer, "tokens": result.tokens}
                cache.save()
            turns.append(Turn(previous, str(cache.data[key]["answer"])))
        visible = recent_turns(turns, args.history_turns)
        raw = turn(dlg.item.question, visible, memory=False)
        mem = turn(dlg.item.question, visible, memory=args.history_turns > 0)
        concat_matches, _ = search(concat_query(dlg.history, dlg.item.question))

        common = {
            "id": dlg.item.id,
            "kind": dlg.kind,
            "type": dlg.item.type,
            "in_corpus": dlg.item.in_corpus,
            # Судье — смысл вопроса: «А у УМНИК?» без диалога не понять.
            "question": dlg.expected_standalone,
            "asked": dlg.item.question,
            "history": HISTORY_SEPARATOR.join(dlg.history),
            "expected_answer": dlg.item.expected_answer,
            "expected_material": dlg.item.expected_material,
        }
        for result, rows in ((raw, rows_raw), (mem, rows_mem)):
            rows.append(
                common
                | {
                    "standalone": result.standalone,
                    "answer": result.answer,
                    "general_answer": result.general,
                    "best_distance": ""
                    if result.best_distance is None
                    else round(result.best_distance, 4),
                    "rank": found_rank(dlg.item, result.matches),
                    "sources": json.dumps(
                        [
                            {"title": m.title, "content": m.content}
                            for m in result.selected
                        ],
                        ensure_ascii=False,
                    ),
                    "tokens": result.tokens,
                    "condense_tokens": result.condense_tokens,
                    "llm_calls": result.calls,
                    "latency_ms": round(result.latency_ms),
                }
            )
        summary_rows.append(
            {
                "kind": dlg.kind,
                "rank_raw": found_rank(dlg.item, raw.matches),
                "rank_concat": found_rank(dlg.item, concat_matches),
                "rank_mem": found_rank(dlg.item, mem.matches),
                "passed_raw": bool(raw.selected),
                "passed_mem": bool(mem.selected),
                "general_raw": raw.general,
                "general_mem": mem.general,
                "condense_unchanged": same_question(mem.standalone, dlg.item.question),
                "tokens_mem": mem.tokens,
            }
        )
        print(
            f"[{number}/{len(dialogues)}] {dlg.item.id} {dlg.kind}: "
            f"«{dlg.item.question}» → «{mem.standalone}»"
        )

    raw_path = results_path(args.out, f"{config}_raw", "e2e")
    mem_path = results_path(args.out, config, "e2e")
    write_csv(raw_path, rows_raw)
    write_csv(mem_path, rows_mem)
    for line in summarize(summary_rows):
        print(line)
    print(f"Результаты: {raw_path}\n            {mem_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
