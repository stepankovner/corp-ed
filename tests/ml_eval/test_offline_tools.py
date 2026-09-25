"""Тесты офлайн-инструментов eval без сети: всё, что ходит в Яндекс, — на подделках."""

import json
from pathlib import Path

import httpx
import pytest

from corp_ed.llm.types import Message, Role
from eval.compare import paired_values
from eval.corpus import ChunkingConfig, chunk_corpus, load_corpus
from eval.generate_silver import (
    FilterStats,
    SeenQuestions,
    SilverQuestion,
    assign_split,
    build_silver_messages,
    copies_chunk,
    filter_questions,
    parse_questions,
    sample_chunks,
)
from eval.judge import (
    Verdict,
    agreement,
    build_judge_messages,
    format_excerpts,
    parse_verdict,
)
from eval.metrics import ThresholdPoint
from eval.probe_embedding_limit import (
    cosine,
    error_message,
    find_cutoff,
    max_accepted,
)
from eval.threshold import (
    candidate_thresholds,
    candidates_for_e2e,
    histogram,
    read_distances,
)
from eval.yandex import (
    EmbeddingCache,
    RateLimiter,
    YandexClient,
    YandexError,
    embed_many,
)

# --- Клиент Яндекса ------------------------------------------------------------------


def _yandex(handler: httpx.MockTransport) -> YandexClient:
    return YandexClient(
        "folder", "key", http=httpx.Client(transport=handler), sleep=lambda _: None
    )


def test_embed_uses_doc_and_query_models() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body["modelUri"])
        assert request.headers["Authorization"] == "Api-Key key"
        return httpx.Response(200, json={"embedding": [0.6, 0.8], "numTokens": "7"})

    client = _yandex(httpx.MockTransport(handler))

    doc = client.embed("текст", "doc")
    client.embed("вопрос", "query")

    assert doc.vector == [0.6, 0.8] and doc.num_tokens == 7
    assert seen == [
        "emb://folder/text-search-doc/latest",
        "emb://folder/text-search-query/latest",
    ]


def test_retries_on_429_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, text="rate limit")
        return httpx.Response(200, json={"embedding": [1.0], "numTokens": 1})

    assert _yandex(httpx.MockTransport(handler)).embed("x", "doc").vector == [1.0]
    assert calls["n"] == 3


def test_client_error_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="text too long")

    with pytest.raises(YandexError) as error:
        _yandex(httpx.MockTransport(handler)).embed("x", "doc")

    assert error.value.status == 400
    assert calls["n"] == 1


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_rate_limiter_spaces_calls() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(4.0, clock=clock, sleep=clock.sleep)

    for _ in range(3):
        limiter.wait()
    clock.now += 10  # долгая пауза: слот свободен, ждать не нужно
    limiter.wait()

    assert clock.sleeps == pytest.approx([0.25, 0.25])


def test_rate_limiter_rejects_non_positive_rate() -> None:
    with pytest.raises(ValueError):
        RateLimiter(0)


def test_embedding_retries_go_through_rate_limiter() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="rate limit")
        return httpx.Response(200, json={"embedding": [1.0], "numTokens": 1})

    client = YandexClient(
        "folder",
        "key",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
        base_delay=0.0,
        embedding_rps=1.0,
    )
    client.embed("x", "doc")

    # Пауза после 429 — 0 (base_delay=0), вторая попытка ждёт слот лимитера.
    assert calls["n"] == 2
    assert any(0.9 < pause <= 1.0 for pause in sleeps)


def test_complete_parses_answer_and_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["modelUri"] == "gpt://folder/yandexgpt/latest"
        assert body["messages"] == [
            {"role": "system", "text": "s"},
            {"role": "user", "text": "u"},
        ]
        return httpx.Response(
            200,
            json={
                "result": {
                    "alternatives": [
                        {
                            "message": {"role": "assistant", "text": "ответ"},
                            "status": "ALTERNATIVE_STATUS_FINAL",
                        }
                    ],
                    "usage": {"inputTextTokens": "12", "completionTokens": "3"},
                    "modelVersion": "x",
                }
            },
        )

    completion = _yandex(httpx.MockTransport(handler)).complete(
        [Message(Role.SYSTEM, "s"), Message(Role.USER, "u")], model="yandexgpt"
    )

    assert (completion.text, completion.input_tokens, completion.output_tokens) == (
        "ответ",
        12,
        3,
    )


def test_complete_via_openai_compatible_api_counts_reasoning() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["OpenAI-Project"] == "folder"
        assert body["model"] == "gpt://folder/gpt-oss-20b/latest"
        assert body["messages"][1] == {"role": "user", "content": "u"}
        assert body["max_tokens"] == 2000
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": " ответ [1] "}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 1800,
                    "completion_tokens": 250,
                    "completion_tokens_details": {"reasoning_tokens": 200},
                },
            },
        )

    completion = _yandex(httpx.MockTransport(handler)).complete(
        [Message(Role.SYSTEM, "s"), Message(Role.USER, "u")],
        model="gpt-oss-20b",
        max_tokens=2000,
        api="openai",
    )

    assert completion.text == "ответ [1]"
    assert (completion.input_tokens, completion.output_tokens) == (1800, 250)
    assert (completion.reasoning_tokens, completion.finish_reason) == (200, "stop")


def test_model_uris() -> None:
    client = YandexClient("folder", "key", embedding_model="text-embeddings-v2")

    assert client.model_uri("doc") == "emb://folder/text-embeddings-v2-doc/latest"
    assert client.gpt_uri("yandexgpt/rc") == "gpt://folder/yandexgpt/rc"
    assert client.gpt_uri("aliceai-llm") == "gpt://folder/aliceai-llm/latest"


def test_embed_many_uses_cache(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["text"])
        return httpx.Response(
            200, json={"embedding": [float(len(calls))], "numTokens": 1}
        )

    client = _yandex(httpx.MockTransport(handler))
    cache = EmbeddingCache(tmp_path / "cache.sqlite")

    first = embed_many(client, ["a", "b"], "doc", cache, workers=1)
    second = embed_many(client, ["a", "b", "c"], "doc", cache, workers=1)

    assert sorted(calls) == ["a", "b", "c"]
    assert [e.vector for e in second[:2]] == [e.vector for e in first]
    # Тот же текст для модели вопросов — другой ключ кэша.
    embed_many(client, ["a"], "query", cache, workers=1)
    assert len(calls) == 4
    cache.close()


# --- Корпус и нарезки ---------------------------------------------------------------


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    folder = tmp_path / "corpus"
    folder.mkdir()
    (folder / "Положение об отпусках.md").write_text(
        "# Положение об отпусках\n\n## Раздел 3\n\n"
        + " ".join(f"Правило номер {i} об отпуске работников." for i in range(40)),
        encoding="utf-8",
    )
    (folder / "Памятка.txt").write_text(
        "Больничный оплачивается по закону.", encoding="utf-8"
    )
    (folder / "игнор.xlsx").write_text("не документ", encoding="utf-8")
    return folder


def test_load_corpus_and_chunk_v2(corpus: Path) -> None:
    documents = load_corpus(corpus)
    chunks = chunk_corpus(
        documents, ChunkingConfig(chunk_tokens=100, overlap_tokens=10)
    )

    assert [d.title for d in documents] == ["Памятка", "Положение об отпусках"]
    assert chunks[0].id == "Памятка#0"
    vacation = [c for c in chunks if c.material == "Положение об отпусках"]
    assert len(vacation) > 1
    assert vacation[0].embed_text.startswith("Положение об отпусках > Раздел 3\n")


def test_chunk_v1_and_no_crumbs(corpus: Path) -> None:
    documents = load_corpus(corpus)

    v1 = chunk_corpus(
        documents, ChunkingConfig(version="v1", chunk_size=300, overlap=30)
    )
    bare = chunk_corpus(documents, ChunkingConfig(crumbs_in_embed=False))

    assert all(c.heading_path == [] and c.embed_text == c.llm_text for c in v1)
    assert not bare[1].embed_text.startswith("Положение об отпусках")
    assert bare[1].llm_text.startswith("Положение об отпусках > Раздел 3\n")


def test_crumbs_without_title(tmp_path: Path) -> None:
    # Название — имя файла, как в демо-корпусе; H1 документа от него отличается.
    (tmp_path / "polozhenie_v2.md").write_text(
        "# Положение об отпусках\n\n## Раздел 3\n\nОтпуск — 28 дней.",
        encoding="utf-8",
    )
    (tmp_path / "Памятка.txt").write_text("Больничный по закону.", encoding="utf-8")
    documents = load_corpus(tmp_path)

    chunks = {
        c.material: c
        for c in chunk_corpus(documents, ChunkingConfig(title_in_crumbs=False))
    }

    regulation = chunks["polozhenie_v2"]
    assert (
        regulation.embed_text == "Положение об отпусках > Раздел 3\nОтпуск — 28 дней."
    )
    assert regulation.llm_text.startswith("polozhenie_v2 > Положение об отпусках")
    # Без заголовков крошки — одно название: в эмбеддинге их не остаётся.
    assert chunks["Памятка"].embed_text == "Больничный по закону."


def test_chunking_config_names() -> None:
    assert ChunkingConfig().name == "v2-400-50"
    assert ChunkingConfig(crumbs_in_embed=False).name == "v2-400-50-nocrumbs"
    assert ChunkingConfig(title_in_crumbs=False).name == "v2-400-50-notitle"
    assert ChunkingConfig(version="v1").name == "v1-1000-100"


# --- BM25 ----------------------------------------------------------------------------


def test_bm25_ranks_by_stemmed_overlap() -> None:
    pytest.importorskip("snowballstemmer")
    from eval.bm25 import BM25Index, tokenize

    index = BM25Index(
        [
            "Ежегодный отпуск составляет 28 календарных дней.",
            "Больничный лист оформляется в электронном виде.",
            "Перенос отпусков на следующий год — по заявлению.",
        ]
    )

    assert tokenize("И в отпуске") == tokenize("отпуск")  # стоп-слова, стемминг
    assert [i for i, _ in index.search("перенести отпуск", 3)] == [2, 0]
    assert index.search("погода в Москве", 3) == []


# --- Сравнение и порог -------------------------------------------------------------


def test_paired_values_intersects_by_id() -> None:
    a = [{"id": "1", "rr": "1.0"}, {"id": "2", "rr": "0.5"}, {"id": "3", "rr": ""}]
    b = [{"id": "2", "rr": "1.0"}, {"id": "1", "rr": "0.0"}, {"id": "4", "rr": "1"}]

    assert paired_values(a, b, "rr") == (["1", "2"], [1.0, 0.5], [0.0, 1.0])


@pytest.mark.parametrize(
    ("low", "high", "p", "expected"),
    [
        (0.07, 0.31, 0.005, "B лучше"),
        (-0.3, -0.1, 0.01, "B хуже"),
        (0.0001, 0.149, 0.12, "в пределах шума"),  # случай с демо-набора
        (-0.03, 0.18, 0.2, "в пределах шума"),
    ],
)
def test_compare_verdict_needs_both_criteria(
    low: float, high: float, p: float, expected: str
) -> None:
    from eval.compare import verdict

    assert expected in verdict(low, high, p)


def test_threshold_helpers() -> None:
    rows = [
        {"in_corpus": "True", "top1_distance": "0.52"},
        {"in_corpus": "False", "top1_distance": "0.7"},
        {"in_corpus": "False", "top1_distance": ""},
    ]

    distances, in_corpus = read_distances(rows, "top1_distance")

    assert distances == [0.52, 0.7, None]
    assert in_corpus == [True, False, False]
    thresholds = candidate_thresholds(distances)
    assert 0.52 in thresholds and 0.3 in thresholds and 0.9 in thresholds
    assert histogram([0.52], [0.7])[0].startswith(" 0.52 | #")


def test_candidates_for_e2e_are_best_and_neighbours() -> None:
    points = [
        ThresholdPoint(t / 100, 1, 1, f, 0.5)
        for t, f in [(50, 0.7), (55, 0.8), (60, 0.9), (65, 0.85), (70, 0.6)]
    ]

    assert [p.threshold for p in candidates_for_e2e(points)] == [0.55, 0.6, 0.65]


# --- Серебряный набор ----------------------------------------------------------------


def test_parse_questions_variants() -> None:
    good = (
        '```json\n{"questions": [{"question": "Когда?", '
        '"evidence": "«Через 14 дней.»"}]}\n```'
    )
    as_list = 'Вот: [{"question": "Кто?", "evidence": "Кадровик."}]'

    assert parse_questions(good) == [SilverQuestion("Когда?", "Через 14 дней.")]
    assert parse_questions(as_list) == [SilverQuestion("Кто?", "Кадровик.")]
    assert parse_questions("не json") == []
    assert parse_questions('{"questions": [{"question": ""}]}') == []


def test_copies_chunk() -> None:
    chunk = (
        "Продолжительность ежегодного основного оплачиваемого отпуска "
        "составляет 28 дней."
    )

    assert copies_chunk(
        "Какова продолжительность ежегодного основного оплачиваемого отпуска?", chunk
    )
    assert not copies_chunk("Сколько дней отдыха дают в году?", chunk)


def test_filter_questions(corpus: Path) -> None:
    chunk = next(
        c
        for c in chunk_corpus(load_corpus(corpus), ChunkingConfig())
        if c.material == "Положение об отпусках"
    )
    stats = FilterStats()
    seen = SeenQuestions()
    evidence = "Правило номер 3 об отпуске работников."
    candidates = [
        SilverQuestion("Что сказано в третьем правиле?", evidence),
        SilverQuestion("Что сказано в третьем правиле?", "Правило номер 4 об отпуске."),
        SilverQuestion("О чём третье правило про отдых?", evidence),
        SilverQuestion("Что с отпуском?", "Отпуск 28 дней."),
        SilverQuestion("Какое правило?", "Правило номер 5."),
    ]

    kept = filter_questions(chunk, candidates, seen, stats)

    assert kept == candidates[:1]
    assert (
        stats.generated,
        stats.kept,
        stats.duplicate,
        stats.bad_evidence,
        stats.short_evidence,
    ) == (5, 1, 2, 1, 1)


def test_seen_questions_compares_evidence_within_one_document() -> None:
    seen = SeenQuestions()
    first = SilverQuestion(
        "Кто организатор?", "Организатор конкурса — Фонд содействия."
    )
    seen.add("Положение", first)
    same_quote = SilverQuestion("Кто проводит конкурс?", first.evidence)

    assert seen.is_duplicate("Положение", same_quote)
    assert not seen.is_duplicate("УМНИК", same_quote)


def test_assign_split_is_deterministic_and_balanced() -> None:
    splits = [assign_split(f"chunk-{i}") for i in range(1000)]

    assert splits == [assign_split(f"chunk-{i}") for i in range(1000)]
    assert 0.25 < splits.count("test") / 1000 < 0.35


def test_silver_prompt_and_sampling(corpus: Path) -> None:
    chunks = chunk_corpus(
        load_corpus(corpus), ChunkingConfig(chunk_tokens=100, overlap_tokens=10)
    )
    sampled = sample_chunks(chunks, 100, seed=1)

    assert all(
        c.material == "Положение об отпусках" for c in sampled
    )  # короткая памятка отсеяна
    assert sampled == sample_chunks(chunks, 100, seed=1)
    user = build_silver_messages(sampled[0], 2)[1].content
    assert "Положение об отпусках > Раздел 3" in user
    assert "evidence" in user


# --- Судья ------------------------------------------------------------------


def test_parse_verdict() -> None:
    assert parse_verdict(
        '```json\n{"correct": 2, "faithful": 1, "reason": "ок"}\n```'
    ) == (Verdict(2, 1, "ок"))
    assert parse_verdict('{"correct": 3, "faithful": 1}') is None
    assert parse_verdict("мусор") is None


def test_judge_prompt_has_mts_rule_and_reference() -> None:
    messages = build_judge_messages(
        "Сколько дней?", "", "Ответа нет.", "(выдержек не было)"
    )

    assert "НЕ ошибка" in messages[0].content
    assert "(пусто — вопрос вне документов)" in messages[1].content


def test_format_excerpts() -> None:
    sources = json.dumps(
        [{"material": "Положение", "heading_path": ["3.1"], "content": "28 дней"}]
    )

    assert format_excerpts(sources) == "[1] Положение > 3.1\n28 дней"
    assert format_excerpts("") == "(выдержек не было)"


def test_agreement() -> None:
    result = agreement([2, 2, 1, 0], [2, 1, 1, 2])

    assert (result.n, result.exact, result.within_one) == (4, 0.5, 0.75)
    assert result.confusion[(2, 1)] == 1


# --- Проба лимита эмбеддера --------------------------------------------------


def _fake_embedder(window: int) -> tuple[list[int], object]:
    """Эмбеддер, который «видит» только первые window символов.

    Вектор — псевдослучайный, но детерминированный по видимому тексту:
    разные префиксы дают заметно разные векторы (как у настоящей модели),
    одинаковый видимый текст — один и тот же вектор.
    """
    import random

    calls: list[int] = []

    def embed_prefix(n: int) -> list[float]:
        calls.append(n)
        rng = random.Random(min(n, window))
        return [rng.uniform(-1, 1) for _ in range(16)]

    return calls, embed_prefix


def test_find_cutoff_detects_silent_truncation() -> None:
    _, embed_prefix = _fake_embedder(window=2048)

    cutoff = find_cutoff(embed_prefix, total_chars=8000, precision=10)  # type: ignore[arg-type]

    assert cutoff is not None and 2048 <= cutoff <= 2058


def test_find_cutoff_without_truncation() -> None:
    _, embed_prefix = _fake_embedder(window=10**9)

    assert find_cutoff(embed_prefix, total_chars=8000) is None  # type: ignore[arg-type]


def test_cosine() -> None:
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_max_accepted_finds_longest_prefix() -> None:
    calls: list[int] = []

    def accepts(n: int) -> bool:
        calls.append(n)
        return n <= 11_531

    assert 11_531 - 40 <= max_accepted(accepts, 8000, 16000) <= 11_531
    assert len(calls) < 10


def test_error_message_strips_session_prefix() -> None:
    body = json.dumps(
        {
            "error": "Error in session internal_id=1&request_id=2&client_id=x: "
            "number of input tokens must be no more than 2048, got 2908",
            "code": 3,
        }
    )

    assert error_message(body) == (
        "number of input tokens must be no more than 2048, got 2908"
    )
    assert error_message("not json") == "not json"


def _chat_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )


def test_completions_go_through_rate_limiter() -> None:
    sleeps: list[float] = []
    client = YandexClient(
        "folder",
        "key",
        http=httpx.Client(transport=httpx.MockTransport(_chat_ok)),
        sleep=sleeps.append,
        completion_rps=1.0,
    )
    messages = [Message(Role.USER, "u")]

    client.complete(messages, api="openai")
    client.complete(messages, api="openai")

    # Второй вызов ждёт следующий слот: не чаще 1 запроса в секунду.
    assert any(0.9 < pause <= 1.0 for pause in sleeps)


def test_completions_respect_concurrency_limit() -> None:
    import threading

    lock = threading.Lock()
    state = {"now": 0, "peak": 0}
    release = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        release.wait(timeout=0.2)
        with lock:
            state["now"] -= 1
        return _chat_ok(request)

    client = YandexClient(
        "folder",
        "key",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        completion_rps=1000.0,
        completion_concurrency=2,
    )
    threads = [
        threading.Thread(
            target=client.complete,
            args=([Message(Role.USER, "u")],),
            kwargs={"api": "openai"},
        )
        for _ in range(5)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Квота каталога — одновременные запросы, а не только частота.
    assert state["peak"] == 2


def test_response_format_is_passed_to_both_apis() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if request.url.path == "/v1/chat/completions":
            return _chat_ok(request)
        return httpx.Response(
            200,
            json={
                "result": {
                    "alternatives": [{"message": {"text": "{}"}, "status": "x"}],
                    "usage": {"inputTextTokens": "1", "completionTokens": "1"},
                }
            },
        )

    client = _yandex(httpx.MockTransport(handler))
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": "a", "schema": schema},
    }
    client.complete(
        [Message(Role.USER, "u")], api="openai", response_format=response_format
    )
    client.complete(
        [Message(Role.USER, "u")], api="native", response_format=response_format
    )
    client.complete(
        [Message(Role.USER, "u")], api="native", response_format={"type": "json_object"}
    )

    assert seen[0]["response_format"] == response_format
    assert seen[1]["jsonSchema"] == {"schema": schema}
    assert seen[2]["jsonObject"] is True


def test_embedding_dim_is_sent_and_kept_apart_in_cache() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"embedding": [1.0, 0.0], "numTokens": "2"})

    client = YandexClient(
        "folder",
        "key",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
        embedding_model="text-embeddings-v2",
        embedding_dim=768,
    )
    client.embed("x", "doc")

    assert seen[0] == {
        "modelUri": "emb://folder/text-embeddings-v2-doc/latest",
        "text": "x",
        "dim": "768",
    }
    # Векторы разной размерности в кэше не смешиваются.
    assert (
        client.cache_uri("doc") == "emb://folder/text-embeddings-v2-doc/latest?dim=768"
    )
