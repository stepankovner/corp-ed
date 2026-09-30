"""M3: реранкер на стенде — пересортировка, кэш, подключение к офлайн-e2e."""

from collections.abc import Sequence
from pathlib import Path

import pytest

from eval.rerank import (
    CachedReranker,
    config_suffix,
    rerank_candidates,
    rerank_ranking,
)


class _FakeEncoder:
    """Балл 1.0 фрагменту со словом «Перенос», остальным 0.1."""

    def __init__(self) -> None:
        self.calls = 0

    def predict(
        self, pairs: Sequence[tuple[str, str]], batch_size: int = 16
    ) -> list[float]:
        self.calls += 1
        return [1.0 if "Перенос" in passage else 0.1 for _, passage in pairs]


def test_rerank_ranking_sorts_by_score_and_keeps_ties_in_order() -> None:
    assert rerank_ranking([7, 3, 5, 9], [0.2, 0.9, 0.2, 0.5]) == [3, 9, 7, 5]
    with pytest.raises(ValueError):
        rerank_ranking([1, 2], [0.5])


def test_rerank_candidates_respects_depth_and_threshold() -> None:
    ranking = [10, 11, 12, 13, 14]
    distance_of = {10: 0.30, 11: 0.40, 12: 0.60, 13: 0.45, 14: 0.35}
    scores = {10: 0.1, 11: 0.2, 13: 0.9, 12: 1.0, 14: 1.0}

    def score(idx: Sequence[int]) -> list[float]:
        return [scores[i] for i in idx]

    # Глубина 4: 14 не пересортировывается; 12 дальше порога — после прошедших.
    assert rerank_candidates(
        ranking, distance_of, score, depth=4, max_distance=0.51
    ) == [13, 11, 10, 12, 14]
    # Без порога 12 с лучшим баллом выходит первым.
    assert rerank_candidates(ranking, distance_of, score, depth=4) == [
        12,
        13,
        11,
        10,
        14,
    ]
    # Никто не прошёл порог — выдача как была.
    assert (
        rerank_candidates(ranking, distance_of, score, depth=4, max_distance=0.1)
        == ranking
    )


def test_cached_reranker_reuses_scores_from_disk(tmp_path: Path) -> None:
    cache = tmp_path / "rerank.json"
    first = CachedReranker(model="m", cache_path=cache)
    encoder = _FakeEncoder()
    first._encoder = encoder

    assert first.score("Вопрос?", ["Перенос отпуска", "Другое"]) == [1.0, 0.1]
    assert first.score("Вопрос?", ["Другое"]) == [0.1]
    assert (encoder.calls, first.scored_pairs) == (1, 2)
    first.save()

    second = CachedReranker(model="m", cache_path=cache)
    second._encoder = _FakeEncoder()
    assert second.score("Вопрос?", ["Перенос отпуска"]) == [1.0]
    assert second._encoder.calls == 0
    # Другая длина пары — другой ключ кэша.
    other = CachedReranker(model="m", max_length=1024, cache_path=cache)
    other._encoder = _FakeEncoder()
    other.score("Вопрос?", ["Перенос отпуска"])
    assert other._encoder.calls == 1
    # int8 даёт другие баллы — свой ключ, прежний кэш fp32 не трогается.
    quantized = CachedReranker(model="m", quantize=True, cache_path=cache)
    quantized._encoder = _FakeEncoder()
    quantized.score("Вопрос?", ["Перенос отпуска"])
    assert quantized._encoder.calls == 1
    assert second._key("Вопрос?", "Перенос отпуска") == first._key(
        "Вопрос?", "Перенос отпуска"
    )


def test_config_suffix() -> None:
    assert config_suffix(30, 512, "embed") == "-rr30"
    assert config_suffix(50, 1024, "llm") == "-rr50-L1024-llmtext"
    assert config_suffix(20, 256, "embed", quantize=True) == "-rr20-L256-int8"


def test_offline_e2e_rerank_puts_best_chunk_into_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eval.offline_e2e as offline_e2e
    from eval.results import read_csv
    from eval.yandex import Completion, YandexClient

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "Положение.md").write_text(
        "# Положение\n\n## Отпуск\n\nОтпуск составляет 28 календарных дней. "
        "Перенос отпуска возможен по заявлению работника.",
        encoding="utf-8",
    )
    dataset = tmp_path / "golden.csv"
    dataset.write_text(
        "id,question,expected_answer,expected_material,expected_section,in_corpus,"
        "type,evidence\n"
        "q1,Можно ли перенести отпуск?,да,Положение,,true,fact,"
        "возможен по заявлению работника\n",
        encoding="utf-8",
    )

    def rankings(
        chunks: Sequence[object],
        queries: Sequence[str],
        limit: int,
        workers: int,
        embedding_model: str = "text-search",
        embedding_dim: int | None = None,
    ) -> tuple[list[list[int]], list[list[float]]]:
        # Вектор ставит первым чанк про 28 дней, нужный — вторым.
        return [[0, 1][:limit] for _ in queries], [[0.3, 0.35][:limit] for _ in queries]

    class _Client:
        def complete(self, messages: Sequence[object], **kwargs: object) -> Completion:
            return Completion(
                text="Да [1].",
                input_tokens=10,
                output_tokens=2,
                latency_ms=5.0,
                model="m",
            )

    encoder = _FakeEncoder()

    def fake_make(model: str, max_length: int) -> CachedReranker:
        reranker = CachedReranker(model=model, max_length=max_length, cache_path=None)
        reranker._encoder = encoder
        return reranker

    monkeypatch.setattr(offline_e2e, "vector_rankings", rankings)
    monkeypatch.setattr(offline_e2e, "make_reranker", fake_make)
    monkeypatch.setattr(YandexClient, "from_env", classmethod(lambda cls: _Client()))

    def run(out: Path, *flags: str) -> dict[str, str]:
        code = offline_e2e.main(
            [
                "--corpus", str(corpus), "--dataset", str(dataset), "--out", str(out),
                "--config", "t", "--limit", "1", "--chunk-tokens", "20",
                "--overlap-tokens", "0", "--not-found", "strict", *flags,
            ]
        )  # fmt: skip
        assert code == 0
        (path,) = out.glob("*_t_e2e.csv")
        return read_csv(path)[0]

    base = run(tmp_path / "base")
    reranked = run(tmp_path / "rr", "--rerank", "--rerank-depth", "2")

    assert base["evidence_in_context"] == "False"
    assert reranked["evidence_in_context"] == "True"
    assert encoder.calls == 1
    # С --rerank нужен чистый вектор: гибрид — ошибка.
    assert (
        offline_e2e.main(
            [
                "--corpus",
                str(corpus),
                "--dataset",
                str(dataset),
                "--rerank",
                "--retriever",
                "hybrid",
                "--out",
                str(tmp_path / "h"),
            ]
        )  # fmt: skip
        == 2
    )
