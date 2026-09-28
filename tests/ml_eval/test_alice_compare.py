"""Сравнение с Алисой (задача 4.3): слепые файлы и отчёт."""

import json
from pathlib import Path

from eval.alice_compare import (
    build_blind,
    cited_materials,
    differences,
    doc_matches,
    format_report,
    is_refusal,
    main,
    split_sources,
    system_stats,
)


def test_is_refusal_covers_both_systems() -> None:
    assert is_refusal("В документах компании ответа нет. Ниже — общая информация: …")
    assert is_refusal("В предоставленных выдержках нет информации о сроках.")
    assert is_refusal(
        "Я поискала в доступных источниках, но не нашла достаточной информации. "
        "Однако известно, что грант — до 5 млн рублей."
    )
    assert is_refusal("В доступных мне источниках нет информации о призовом фонде.")
    assert is_refusal("Не получилось найти ответ, задайте другой вопрос")
    assert not is_refusal("Максимальный размер гранта — до 5 млн рублей [1].")
    assert not is_refusal("Да, работы можно закончить быстрее года.")


def test_doc_matches_by_prefix_and_sources_split() -> None:
    expected = "Положение Старт-ИИ-1 (очередь 2)_на сайт"
    assert doc_matches("Положение СТАРТ-ИИ-1", expected)
    assert doc_matches(expected, expected)
    assert not doc_matches("Победители_Старт-ИИ-1", expected)
    assert not doc_matches("UMNIK-2026", expected)
    assert not doc_matches("", expected)
    assert split_sources("Положение СТАРТ-ИИ-1; UMNIK-2026") == [
        "Положение СТАРТ-ИИ-1",
        "UMNIK-2026",
    ]
    assert split_sources("нет") == []
    assert split_sources("") == []


def test_cited_materials_follow_citation_numbers() -> None:
    sources = json.dumps(
        [{"material": "UMNIK-2026"}, {"material": "Положение Старт-ИИ-1"}]
    )
    assert cited_materials("Срок 12 месяцев [2].", sources) == (
        True,
        ["Положение Старт-ИИ-1"],
    )
    assert cited_materials("Ответ без ссылок.", sources) == (False, [])
    assert cited_materials("Ссылка в никуда [7].", sources) == (True, [])


def _ours(row_id: str, answer: str, in_corpus: str = "true") -> dict[str, str]:
    return {
        "id": row_id,
        "type": "fact",
        "in_corpus": in_corpus,
        "question": f"Вопрос {row_id}?",
        "expected_answer": "5 млн" if in_corpus == "true" else "",
        "expected_material": "Положение Старт-ИИ-1 (очередь 2)_на сайт",
        "answer": answer,
        "sources": json.dumps(
            [{"material": "Положение Старт-ИИ-1 (очередь 2)_на сайт"}]
        ),
        "latency_ms": "1500",
    }


def _alice(row_id: str, answer: str, source: str = "нет") -> dict[str, str]:
    return {
        "id": row_id,
        "question": f"Вопрос {row_id}?",
        "in_corpus": "true",
        "answer": answer,
        "source": source,
        "seconds": "4.2",
        "asked_at": "",
        "notes": "",
    }


def test_build_blind_pairs_common_questions_and_hides_sources() -> None:
    ours = [
        _ours("d01", "**До 5 млн** рублей [1]."),
        _ours("d02", "Без пары у Алисы."),
        _ours(
            "o01",
            "В документах компании ответа нет. Ниже — общая информация: …",
            "false",
        ),
    ]
    alice = [
        _alice(
            "d01", "Я — Алиса. До 5 млн рублей 🙂", "Положение СТАРТ-ИИ-1; UMNIK-2026"
        ),
        _alice("o01", "Я поискала в доступных источниках, но не нашла информации."),
        _alice("d03", ""),
    ]

    ours_rows, alice_rows = build_blind(ours, alice)

    assert [r["id"] for r in ours_rows] == ["d01", "o01"]
    assert [r["id"] for r in alice_rows] == ["d01", "o01"]
    assert all(r["sources"] == "" for r in [*ours_rows, *alice_rows])
    assert ours_rows[0]["answer"] == "До 5 млн рублей."
    assert alice_rows[0]["answer"] == "До 5 млн рублей"
    assert ours_rows[0]["has_link"] and ours_rows[0]["source_ok"]
    assert alice_rows[0]["has_link"] and not alice_rows[0]["source_ok"]
    assert alice_rows[0]["cited_docs"] == "Положение СТАРТ-ИИ-1; UMNIK-2026"
    assert ours_rows[1]["refusal"] and alice_rows[1]["refusal"]
    assert ours_rows[0]["latency_s"] == "1.50"
    assert alice_rows[0]["latency_s"] == "4.2"
    assert alice_rows[1]["expected_answer"] == ""


def _judged(
    row_id: str, in_corpus: str, refusal: str, judge: str, source_ok: str = "False"
) -> dict[str, str]:
    return {
        "id": row_id,
        "in_corpus": in_corpus,
        "refusal": refusal,
        "has_link": source_ok,
        "source_ok": source_ok,
        "latency_s": "2.0",
        "judge_correct": judge,
    }


def test_system_stats_and_differences() -> None:
    ours = [
        _judged("a", "true", "False", "2", "True"),
        _judged("b", "true", "False", "1"),
        _judged("c", "true", "True", "0"),
        _judged("x", "false", "True", "2"),
        _judged("y", "false", "False", "0"),
    ]
    alice = [
        _judged("a", "true", "False", "2"),
        _judged("b", "true", "False", "0"),
        _judged("c", "true", "False", ""),
        _judged("x", "false", "True", "2"),
        _judged("y", "false", "True", "2"),
    ]

    stats = system_stats(ours)
    assert (stats.n_in, stats.n_out) == (3, 2)
    assert (stats.correct, stats.partial, stats.wrong, stats.unjudged) == (1, 1, 1, 0)
    assert stats.correct_general == 0
    assert stats.score == 1.0
    assert stats.false_out == 1 and stats.judge_false_out == 1
    assert (stats.answered_in, stats.linked, stats.link_ok) == (2, 1, 1)
    assert stats.recall == 2 / 3 and stats.precision == 2 / 3
    assert system_stats(alice).unjudged == 1
    general = [_judged("g", "true", "True", "2")]
    assert system_stats(general).correct_general == 1

    by_metric = {d.metric: d for d in differences(ours, alice)}
    assert by_metric["score"].n == 2  # «c» без вердикта у Алисы — не в паре
    assert by_metric["score"].delta == 0.5
    assert by_metric["false_out"].n == 2 and by_metric["false_out"].delta == 0.5
    assert by_metric["link_ok"].delta == 1 / 3
    assert by_metric["latency"].n == 5 and by_metric["latency"].delta == 0.0
    assert by_metric["score"].verdict in ("шум", "мы лучше, вне шума")

    report = format_report(ours, alice, title="Проверка")
    assert "### Проверка" in report
    assert "| верно (2) по корпусу | 1/3 | 1/3 |" in report
    assert "| ссылка на нужный документ | 1/2 | 0/3 |" in report


def test_verdict_direction_depends_on_metric() -> None:
    from eval.alice_compare import Difference

    faster = Difference("latency", 10, 1.0, 6.0, -5.0, -7.0, -3.0, 0.001, 0, 10)
    assert faster.verdict == "мы лучше, вне шума"
    worse = Difference("score", 10, 1.0, 1.6, -0.6, -0.9, -0.2, 0.01, 1, 8)
    assert worse.verdict == "мы хуже, вне шума"
    assert Difference("score", 10, 1.5, 1.5, 0.0, -0.3, 0.3, 0.9, 3, 3).verdict == "шум"


def test_report_cli_excludes_ids_and_writes_markdown(tmp_path: Path) -> None:
    from eval.results import write_csv

    ours = tmp_path / "ours_judged.csv"
    alice = tmp_path / "alice_judged.csv"
    write_csv(
        ours, [_judged("a", "true", "False", "2"), _judged("b", "true", "False", "0")]
    )
    write_csv(
        alice, [_judged("a", "true", "False", "0"), _judged("b", "true", "False", "2")]
    )
    out = tmp_path / "report.md"

    assert (
        main(
            [
                "report",
                "--ours",
                str(ours),
                "--alice",
                str(alice),
                "--exclude",
                "b",
                "--md",
                str(out),
            ]
        )
        == 0
    )
    text = out.read_text(encoding="utf-8")
    assert "| верно (2) по корпусу | 1/1 | 0/1 |" in text


def test_blind_cli_writes_two_files(tmp_path: Path) -> None:
    from eval.results import write_csv

    ours = tmp_path / "ours_e2e.csv"
    alice = tmp_path / "alice.csv"
    write_csv(ours, [_ours("d01", "До 5 млн рублей [1].")])
    write_csv(alice, [_alice("d01", "До 5 млн рублей.", "Положение СТАРТ-ИИ-1")])
    out_dir = tmp_path / "blind"

    assert (
        main(
            [
                "blind",
                "--ours",
                str(ours),
                "--alice",
                str(alice),
                "--out-dir",
                str(out_dir),
                "--day",
                "2026-09-26",
            ]
        )
        == 0
    )
    assert (out_dir / "2026-09-26_ours-blind_e2e.csv").exists()
    assert (out_dir / "2026-09-26_alice-blind_e2e.csv").exists()
    write_csv(alice, [_alice("d09", "")])
    assert (
        main(
            [
                "blind",
                "--ours",
                str(ours),
                "--alice",
                str(alice),
                "--out-dir",
                str(out_dir),
            ]
        )
        == 1
    )
