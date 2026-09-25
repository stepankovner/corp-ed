"""Офлайн-e2e без сети: поиск и YandexGPT подменены."""

from collections.abc import Sequence
from pathlib import Path

import pytest

import eval.offline_e2e as offline_e2e
from corp_ed.llm.types import Message
from corp_ed.prompts.faq import GENERAL_ANSWER_PREFIX, NOT_FOUND_ANSWER
from eval.offline_e2e import (
    OfflineMatch,
    citation_numbers,
    gate_by_best_distance,
    invalid_citations,
    relevant_matches,
    section_citations,
)
from eval.results import read_csv
from eval.yandex import Completion, YandexClient

GOLDEN = (
    "id,question,expected_answer,expected_material,expected_section,in_corpus,type\n"
    "q1,Сколько дней отпуска?,28,Положение,,true,fact\n"
    "q2,Какая погода завтра?,,,,false,out_of_corpus\n"
    "q3,Можно ли перенести отпуск?,,Положение,,true,negation\n"
)


def test_relevant_matches_keeps_order_and_threshold() -> None:
    matches = [
        OfflineMatch("a", "Д", distance=0.5),
        OfflineMatch("b", "Д", distance=0.7),
        OfflineMatch("c", "Д", distance=0.6),
    ]

    assert [m.content for m in relevant_matches(matches, 0.6)] == ["a", "c"]


def test_citations() -> None:
    answer = "Отпуск 28 дней [1], перенос по заявлению [2][4]."

    assert citation_numbers(answer) == [1, 2, 4]
    assert invalid_citations(answer, n_sources=2) == [4]
    assert invalid_citations("Без ссылок.", n_sources=0) == []


def test_section_citations_are_not_excerpt_numbers() -> None:
    answer = "Срок 12 месяцев [2.2], может быть уменьшен [6.1.1.] и [1]."

    assert section_citations(answer) == 2
    assert citation_numbers(answer) == [1]


def test_defaults_are_the_task1_decision() -> None:
    from eval.yandex import default_embedding_dim

    args = offline_e2e._parser().parse_args(["--corpus", "c", "--dataset", "d"])

    # Решение 25.09: Flash через OpenAI-совместимый API, v2-768, порог 0.51.
    assert (args.model, args.api, args.embedding_model, args.max_distance) == (
        "aliceai-llm-flash",
        "openai",
        "text-embeddings-v2",
        0.51,
    )
    assert default_embedding_dim(args.embedding_model) == 768
    assert default_embedding_dim("text-search") is None
    # Р1 (25.09): ответа в документах нет — общий ответ с пометкой.
    assert args.not_found == "general"


class _FakeYandex:
    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.prompts: list[str] = []

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float,
        max_tokens: int = 1000,
        api: str = "native",
    ) -> Completion:
        user = messages[-1].content
        self.prompts.append(user)
        text = next(
            (answer for key, answer in self.answers.items() if key in user),
            "Ответ [1].",
        )
        return Completion(
            text=text, input_tokens=100, output_tokens=10, latency_ms=50.0, model=model
        )


@pytest.fixture
def setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, _FakeYandex]:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "Положение.md").write_text(
        "# Положение\n\n## Отпуск\n\nОтпуск составляет 28 календарных дней.",
        encoding="utf-8",
    )
    dataset = tmp_path / "golden.csv"
    dataset.write_text(GOLDEN, encoding="utf-8")

    def fake_rankings(
        chunks: Sequence[object],
        queries: Sequence[str],
        limit: int,
        workers: int,
        embedding_model: str = "text-search",
        embedding_dim: int | None = None,
    ) -> tuple[list[list[int]], list[list[float]]]:
        distances = {"Сколько": 0.3, "Какая": 0.8, "Можно": 0.4}
        return (
            [[0] for _ in queries],
            [[distances[q.split()[0]]] for q in queries],
        )

    fake = _FakeYandex(
        {
            "Сколько дней": "28 календарных дней [1] по пункту [2.2]. См. [3].",
            "перенести": "В предоставленных выдержках нет информации о переносе.",
            "погода": "Завтра ясно.",
        }
    )
    monkeypatch.setattr(offline_e2e, "vector_rankings", fake_rankings)
    monkeypatch.setattr(YandexClient, "from_env", classmethod(lambda cls: fake))
    return corpus, dataset, fake


def _run(corpus: Path, dataset: Path, out: Path, *flags: str) -> list[dict[str, str]]:
    code = offline_e2e.main(
        [
            "--corpus",
            str(corpus),
            "--dataset",
            str(dataset),
            "--out",
            str(out),
            "--config",
            "test",
            *flags,
        ]
    )
    assert code == 0
    (path,) = out.glob("*_test_e2e.csv")
    return read_csv(path)


def test_strict_mode_refuses_without_llm(
    setup: tuple[Path, Path, _FakeYandex], tmp_path: Path
) -> None:
    corpus, dataset, fake = setup

    rows = {
        row["id"]: row
        for row in _run(corpus, dataset, tmp_path / "out", "--not-found", "strict")
    }

    # q2 дальше порога 0.51: LLM не вызывается, фиксированная фраза отказа.
    assert len(fake.prompts) == 2
    assert rows["q2"]["answer"] == NOT_FOUND_ANSWER
    assert rows["q2"]["answered"] == "False" and rows["q2"]["n_sources"] == "0"
    # q1: модель сослалась на несуществующую выдержку [3] — считаем по сырому
    # ответу; normalize_citations превратила её в текст «(п. 3)», ссылка одна.
    assert rows["q1"]["answered"] == "True"
    assert (rows["q1"]["citations"], rows["q1"]["invalid_citations"]) == ("1", "1")
    # [2.2] — номер пункта: в выдержке его нет, ответ нормализован в текст.
    assert rows["q1"]["section_citations"] == "1"
    assert "по пункту (п. 2.2)" in rows["q1"]["answer"]
    assert "Отпуск составляет 28" in rows["q1"]["sources"]
    # q3: отказ своими словами — не «ответил», но нарушение формата.
    assert rows["q3"]["answered"] == "False"
    assert rows["q3"]["refusal_paraphrase"] == "True"
    summary = read_csv(tmp_path / "out" / "summary.csv")
    assert summary[-1]["mode"] == "offline-e2e"


def test_general_mode_answers_with_prefix(
    setup: tuple[Path, Path, _FakeYandex], tmp_path: Path
) -> None:
    corpus, dataset, fake = setup

    rows = {
        row["id"]: row
        for row in _run(corpus, dataset, tmp_path / "out", "--not-found", "general")
    }

    assert len(fake.prompts) == 3
    assert rows["q2"]["answer"].startswith(GENERAL_ANSWER_PREFIX)
    # Общий ответ — не ответ по документам: для F1 отказа это отказ.
    assert rows["q2"]["answered"] == "False"
    assert (rows["q2"]["general_answer"], rows["q2"]["general_after_refusal"]) == (
        "True",
        "False",
    )
    # q3: отказ своими словами — не NOT_FOUND_ANSWER, второго вызова нет.
    assert rows["q3"]["general_answer"] == "False"


def test_general_answer_after_model_refusal(
    setup: tuple[Path, Path, _FakeYandex], tmp_path: Path
) -> None:
    corpus, dataset, fake = setup
    # По выдержкам (перед вопросом — пустая строка) модель отказывает фразой
    # NOT_FOUND_ANSWER, в общем промпте вопрос идёт первой строкой.
    fake.answers = {
        "\n\nВопрос сотрудника: Можно ли перенести": NOT_FOUND_ANSWER,
        "Вопрос сотрудника: Можно ли перенести": "Обычно перенос согласуют заранее.",
        **fake.answers,
    }

    rows = {row["id"]: row for row in _run(corpus, dataset, tmp_path / "out")}

    # q1 — ответ по выдержкам, q2 — общий без выдержек, q3 — два вызова.
    assert len(fake.prompts) == 4
    q3 = rows["q3"]
    assert q3["answer"] == (
        f"{GENERAL_ANSWER_PREFIX}\nОбычно перенос согласуют заранее."
    )
    assert (q3["general_answer"], q3["general_after_refusal"]) == ("True", "True")
    assert q3["answered"] == "False" and q3["n_sources"] == "1"
    # Токены и задержка — сумма двух вызовов.
    assert (q3["llm_calls"], q3["input_tokens"], q3["latency_ms"]) == (
        "2",
        "200",
        "100",
    )


def test_gate_by_best_distance() -> None:
    matches = [
        OfflineMatch("a", "Д", distance=0.5),
        OfflineMatch("b", "Д", distance=None),
    ]

    # Гибрид: у чанка из BM25 расстояния нет — решает лучший векторный.
    assert gate_by_best_distance(matches, 0.5, 0.6) == matches
    assert gate_by_best_distance(matches, 0.7, 0.6) == []
    assert gate_by_best_distance(matches, None, 0.6) == []
    # Вектор: порог на каждый чанк, None не проходит.
    assert relevant_matches(matches, 0.6) == matches[:1]


def test_hybrid_mode_gates_by_vector_distance(
    setup: tuple[Path, Path, _FakeYandex], tmp_path: Path
) -> None:
    corpus, dataset, fake = setup

    rows = {
        row["id"]: row
        for row in _run(
            corpus,
            dataset,
            tmp_path / "out",
            "--retriever",
            "hybrid",
            "--not-found",
            "strict",
        )
    }

    # q2: BM25 что-то нашёл бы, но лучший вектор 0.8 > 0.6 — отказ без LLM.
    assert rows["q2"]["answer"] == NOT_FOUND_ANSWER and len(fake.prompts) == 2
    assert rows["q1"]["n_sources"] == "1"
    assert float(rows["q1"]["best_distance"]) == pytest.approx(0.3)
