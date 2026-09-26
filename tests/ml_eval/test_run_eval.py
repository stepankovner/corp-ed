"""run_eval целиком против поддельного API (httpx.MockTransport) — без бэкенда."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from eval import run_eval
from eval.api_client import CorpEdClient, parse_search_response
from eval.datasets import GOLDEN_COLUMNS
from eval.results import read_csv, write_csv

VACATION_CHUNK = {
    "chunk_id": "c-31",
    "material_id": "m1",
    "material_title": "Положение об отпусках.docx",
    "position": 4,
    "heading_path": ["Раздел 3", "3.1 Продолжительность"],
    "llm_text": (
        "Положение об отпусках > Раздел 3 > 3.1 Продолжительность\nОтпуск — 28 дней."
    ),
    "distance": 0.52,
}
OTHER_CHUNK = {
    "id": "c-99",
    "title": "Памятка",
    "position": 1,
    "heading_path": "Прочее > Контакты",
    "content": "Телефон офиса.",
    "distance": 0.71,
}


def _api(request: httpx.Request) -> httpx.Response:
    body: Any = json.loads(request.content or b"{}")
    path = request.url.path
    if path == "/api/v1/auth/login":
        return httpx.Response(200, json={"access_token": "t0k", "token_type": "bearer"})
    assert request.headers["Authorization"] == "Bearer t0k"
    if path == "/api/v1/faq/search":
        if "отпуск" in body["question"].lower():
            return httpx.Response(200, json={"matches": [OTHER_CHUNK, VACATION_CHUNK]})
        return httpx.Response(200, json={"matches": [OTHER_CHUNK]})
    if path == "/api/v1/faq/ask":
        if "отпуск" in body["question"].lower():
            return httpx.Response(
                200,
                json={
                    "content": "Отпуск — 28 календарных дней [1].",
                    "answer_given": True,
                    "sources": [VACATION_CHUNK],
                    "model": "yandexgpt-lite",
                },
            )
        return httpx.Response(
            200,
            json={
                "content": "В документах компании ответа нет.",
                "answer_given": False,
                "sources": [],
            },
        )
    return httpx.Response(404)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> CorpEdClient:
    http = httpx.Client(base_url="http://test", transport=httpx.MockTransport(_api))
    api = CorpEdClient(http=http)
    api.login("acme", "admin@acme.ru", "secret")
    monkeypatch.setattr(CorpEdClient, "from_env", classmethod(lambda cls: api))
    return api


@pytest.fixture
def golden(tmp_path: Path) -> Path:
    rows = [
        [
            "g01",
            "Сколько дней отпуска?",
            "28 календарных дней",
            "Положение об отпусках",
            "3.1",
            "true",
            "fact",
        ],
        ["o01", "Какая погода в Москве?", "", "", "", "false", "out_of_corpus"],
    ]
    lines = [",".join(GOLDEN_COLUMNS)] + [
        ",".join(f'"{c}"' for c in row) for row in rows
    ]
    path = tmp_path / "golden.csv"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_parse_search_response_variants() -> None:
    chunks = parse_search_response({"matches": [VACATION_CHUNK, OTHER_CHUNK]})

    assert [c.id for c in chunks] == ["c-31", "c-99"]
    assert chunks[0].material == "Положение об отпусках.docx"
    assert chunks[0].content.endswith("Отпуск — 28 дней.")
    assert chunks[1].heading_path == ["Прочее", "Контакты"]
    assert parse_search_response([OTHER_CHUNK])[0].distance == 0.71


def test_parse_search_response_without_ids() -> None:
    chunk = parse_search_response(
        [{"material_id": "m1", "position": 7, "content": "x"}]
    )[0]

    assert chunk.id == "m1:7"
    assert chunk.distance is None


def test_parse_search_response_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_search_response({"detail": "Not Found"})


def test_retrieval_mode(client: CorpEdClient, golden: Path, tmp_path: Path) -> None:
    out = tmp_path / "results"

    code = run_eval.main(
        [
            "retrieval",
            "--dataset",
            str(golden),
            "--config",
            "v2-400-50",
            "--out",
            str(out),
        ]
    )

    assert code == 0
    [results] = [p for p in out.glob("*_v2-400-50_retrieval.csv")]
    rows = read_csv(results)
    assert rows[0]["gold_rank"] == "2"
    assert rows[0]["top1_distance"] == "0.7100"
    assert rows[1]["in_corpus"] == "False"
    summary = read_csv(out / "summary.csv")
    assert summary[0]["mode"] == "retrieval"
    assert summary[0]["hit@1"] == "0.0000"
    assert summary[0]["hit@3"] == "1.0000"
    assert summary[0]["mrr"] == "0.5000"


def test_e2e_and_score_modes(
    client: CorpEdClient, golden: Path, tmp_path: Path
) -> None:
    out = tmp_path / "results"

    run_eval.main(
        ["e2e", "--dataset", str(golden), "--config", "lite-k5", "--out", str(out)]
    )

    [results] = list(out.glob("*_lite-k5_e2e.csv"))
    rows = read_csv(results)
    assert [row["answered"] for row in rows] == ["True", "False"]
    assert json.loads(rows[0]["sources"])[0]["material"] == "Положение об отпусках.docx"
    assert json.loads(rows[0]["extra"]) == {"model": "yandexgpt-lite"}
    e2e_summary = read_csv(out / "summary.csv")[0]
    assert (e2e_summary["precision"], e2e_summary["recall"]) == ("1.0000", "1.0000")

    # Ручная разметка: ответ по отпуску верный.
    labeled = [dict(row) for row in rows]
    labeled[0]["correct"] = "2"
    write_csv(results, labeled)

    run_eval.main(["score", "--results", str(results), "--out", str(out)])

    scored = read_csv(out / "summary.csv")[-1]
    assert scored["mode"] == "e2e-scored"
    assert scored["correct_rate"] == "1.0000"


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("В предоставленных выдержках нет информации об этом.", True),
        ("Информация об этом в документах отсутствует.", True),
        ("Документы не содержат ответа на этот вопрос.", True),
        ("В документах компании ответа нет.", False),  # это канонический отказ
        ("Нет. Перенос возможен только по заявлению [1].", False),
        ("В выдержках нет данных о бухгалтерах, но для продаж лимит есть [1].", False),
        ("Отпуск составляет 28 дней [1].", False),
    ],
)
def test_looks_like_refusal(answer: str, expected: bool) -> None:
    assert run_eval.looks_like_refusal(answer) is expected


def test_is_answered() -> None:
    assert run_eval.is_answered("Отпуск — 28 дней [1].", answer_given=True)
    assert not run_eval.is_answered("Отпуск — 28 дней [1].", answer_given=False)
    assert not run_eval.is_answered(
        "В документах компании ответа нет.", answer_given=True
    )
    assert not run_eval.is_answered(
        "В выдержках нет такой информации.", answer_given=True
    )


def test_score_rows_auto_labels_out_of_corpus() -> None:
    rows = [
        {
            "id": "g1",
            "type": "fact",
            "in_corpus": "True",
            "answered": "True",
            "correct": "2",
        },
        {
            "id": "g2",
            "type": "fact",
            "in_corpus": "True",
            "answered": "True",
            "correct": "1",
        },
        {
            "id": "o1",
            "type": "out_of_corpus",
            "in_corpus": "False",
            "answered": "False",
            "correct": "",
        },
        {
            "id": "o2",
            "type": "out_of_corpus",
            "in_corpus": "False",
            "answered": "True",
            "correct": "",
        },
        {
            "id": "g3",
            "type": "negation",
            "in_corpus": "True",
            "answered": "True",
            "correct": "",
        },
    ]

    score = run_eval.score_rows(rows)

    assert score.unlabeled == ["g3"]
    assert score.in_corpus_correct_rate == 0.5
    assert score.false_answer_rate == 0.5
    assert score.correct_rate == 0.5
    assert not score.quality_bar_passed
    assert score.by_type == {"fact": 0.5, "out_of_corpus": 0.5}


def test_score_rows_rejects_bad_label() -> None:
    rows = [
        {
            "id": "g1",
            "type": "fact",
            "in_corpus": "True",
            "answered": "True",
            "correct": "5",
        }
    ]

    with pytest.raises(ValueError):
        run_eval.score_rows(rows)
