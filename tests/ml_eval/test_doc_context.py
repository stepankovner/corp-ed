"""Контекст документа на стенде: извлечение с кэшем, нарезка, флаги (без сети)."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from corp_ed.domain.doc_context import CONTEXT_PREFIX, DocContext
from corp_ed.llm.types import Message
from eval import bench, offline_e2e
from eval.corpus import ChunkingConfig, chunk_corpus, load_corpus, section_corpus
from eval.doc_context import (
    attach_contexts,
    corpus_statuses,
    extract_contexts,
)
from eval.yandex import Completion

CARD = DocContext(
    title="Положение о командировках",
    organization="СТК",
    program="—",
    track="—",
    year="2026",
    edition="—",
)


class FakeClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[list[Message]] = []
        self.kwargs: list[dict[str, Any]] = []

    def complete(self, messages: Sequence[Message], **kwargs: Any) -> Completion:
        self.calls.append(list(messages))
        self.kwargs.append(kwargs)
        return Completion(
            text=self.text,
            input_tokens=700,
            output_tokens=60,
            latency_ms=1.0,
            model=kwargs["model"],
        )


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    folder = tmp_path / "corpus"
    folder.mkdir()
    (folder / "komandirovki 2025.md").write_text(
        "# Положение о командировках\n\n## Суточные\n\nСуточные — 1 200 руб.",
        encoding="utf-8",
    )
    (folder / "komandirovki 2026.md").write_text(
        "# Положение о командировках\n\n## Суточные\n\nСуточные — 1 450 руб.",
        encoding="utf-8",
    )
    return folder


def test_extract_uses_cache_and_counts_tokens(corpus: Path, tmp_path: Path) -> None:
    documents = load_corpus(corpus)
    client = FakeClient(CARD.model_dump_json())
    cache = tmp_path / "cache.json"

    first = extract_contexts(client, documents, model="m", cache_path=cache)
    assert len(client.calls) == 2
    assert client.kwargs[0]["response_format"]["json_schema"]["strict"] is True
    assert client.kwargs[0]["temperature"] == 0.0
    assert first["komandirovki 2026"].context == CARD
    assert first["komandirovki 2026"].input_tokens == 700
    assert not first["komandirovki 2026"].cached

    second = extract_contexts(client, documents, model="m", cache_path=cache)
    assert len(client.calls) == 2  # всё из кэша
    assert second["komandirovki 2025"].cached
    assert second["komandirovki 2025"].context == CARD
    # Другая модель — другой ключ.
    extract_contexts(client, documents, model="other", cache_path=cache)
    assert len(client.calls) == 4


def test_extract_bad_schema_gives_no_context(corpus: Path, tmp_path: Path) -> None:
    documents = load_corpus(corpus)
    client = FakeClient('{"title": "только название"}')
    found = extract_contexts(
        client, documents, model="m", cache_path=tmp_path / "c.json"
    )
    assert all(e.context is None for e in found.values())
    attached = attach_contexts(documents, found, {})
    assert all(d.context == "" for d in attached)


def test_attach_and_chunk_with_context(corpus: Path, tmp_path: Path) -> None:
    documents = load_corpus(corpus)
    found = extract_contexts(
        FakeClient(CARD.model_dump_json()),
        documents,
        model="m",
        cache_path=tmp_path / "c.json",
    )
    statuses = corpus_statuses(documents)
    assert statuses == {
        "komandirovki 2026": "current",
        "komandirovki 2025": "superseded",
    }
    attached = attach_contexts(documents, found, statuses)
    old = next(d for d in attached if d.title == "komandirovki 2025")
    assert old.context.startswith(CONTEXT_PREFIX)
    assert "заменена" in old.context

    plain = chunk_corpus(attached, ChunkingConfig())
    embed = chunk_corpus(attached, ChunkingConfig(doc_context="embed"))
    both = chunk_corpus(attached, ChunkingConfig(doc_context="both"))
    llm = chunk_corpus(attached, ChunkingConfig(doc_context="llm"))
    assert [c.embed_text for c in llm] == [c.embed_text for c in plain]
    assert [c.llm_text for c in llm] == [c.llm_text for c in both]
    assert [c.embed_text for c in plain] == [
        c.embed_text for c in chunk_corpus(documents, ChunkingConfig())
    ]
    crumbs = "komandirovki 2025 > Положение о командировках > Суточные"
    assert embed[0].embed_text == f"{crumbs}\n{old.context}\nСуточные — 1 200 руб."
    assert embed[0].llm_text == plain[0].llm_text
    assert both[0].llm_text == f"{crumbs}\n{old.context}\nСуточные — 1 200 руб."
    assert ChunkingConfig(doc_context="embed").name == "v2-400-50-ctx"
    assert ChunkingConfig(doc_context="llm").name == "v2-400-50-ctxllm"
    assert ChunkingConfig(doc_context="both").name == "v2-400-50-ctxboth"

    # Без крошек (E2) крошки срезаются, контекст остаётся.
    bare = chunk_corpus(
        attached, ChunkingConfig(crumbs_in_embed=False, doc_context="embed")
    )
    assert bare[0].embed_text == f"{old.context}\nСуточные — 1 200 руб."

    # Секции (M2) — тот же llm_text у чанков, иначе select_sections их не найдёт.
    sections = section_corpus(attached, ChunkingConfig(doc_context="llm"))
    section = sections[both[0].section_id]
    assert both[0].llm_text in section.chunks
    assert old.context in section.content


def test_flags_parse() -> None:
    args = bench._parser().parse_args(
        ["--corpus", "c", "--dataset", "d", "--doc-context", "embed"]
    )
    assert args.doc_context == "embed"
    assert args.doc_context_provider == "yandex"
    args = offline_e2e._parser().parse_args(
        ["--corpus", "c", "--dataset", "d", "--doc-context", "both"]
    )
    assert args.doc_context == "both"
    assert (
        offline_e2e._parser()
        .parse_args(["--corpus", "c", "--dataset", "d"])
        .doc_context
        == "off"
    )
