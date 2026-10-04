"""LLM_PROVIDER=fake: разработка фронта и сквозные тесты без ключей."""

import asyncio
import time
from dataclasses import dataclass

import httpx
import pytest
from pydantic import ValidationError

from corp_ed.core.config import LLMSettings
from corp_ed.llm.factory import build_embedding_gateway, build_llm_gateway
from corp_ed.llm.fake import DevAdapter
from corp_ed.llm.fake_embedding import WordEmbeddingAdapter
from corp_ed.llm.throttle import Throttle, ThrottleBusyError
from corp_ed.llm.types import Message, Role
from corp_ed.prompts.faq import build_faq_messages, build_general_messages


@dataclass
class _Chunk:
    title: str
    heading_path: list[str]
    content: str


def test_fake_provider_needs_no_keys_and_is_refused_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("YC_FOLDER_ID", "YC_API_KEY", "ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    assert LLMSettings().llm_provider == "fake"

    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(ValidationError, match="development only"):
        LLMSettings()


def test_real_provider_still_requires_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("YC_FOLDER_ID", "YC_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError, match="YC_FOLDER_ID and YC_API_KEY"):
        LLMSettings()


async def test_factory_builds_dev_gateways(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    settings = LLMSettings()
    async with httpx.AsyncClient() as client:
        assert isinstance(build_llm_gateway(client, settings), DevAdapter)
        assert isinstance(
            build_embedding_gateway(client, settings), WordEmbeddingAdapter
        )


async def test_dev_answer_quotes_the_first_excerpt_with_a_citation() -> None:
    chunk = _Chunk(
        "Положение об отпусках",
        ["Отпуск"],
        "Ежегодный отпуск — 28 календарных дней.\nДелится на части.",
    )
    other = _Chunk("Другое", [], "Не то.")
    completion = await DevAdapter().generate(
        build_faq_messages("Сколько дней отпуска?", [chunk, other])
    )
    assert completion.content.endswith("[1]")
    assert "28 календарных дней" in completion.content
    assert "Не то" not in completion.content
    assert completion.usage.input_tokens > 0

    general = await DevAdapter().generate(build_general_messages("Погода?"))
    assert "[1]" not in general.content
    assert (await DevAdapter().generate([Message(role=Role.USER, content="")])).content


async def test_word_embeddings_bring_shared_words_together() -> None:
    adapter = WordEmbeddingAdapter()
    doc = (await adapter.embed_document("Отпуск 28 календарных дней")).embedding
    close = (await adapter.embed_query("сколько дней отпуск")).embedding
    far = (await adapter.embed_query("погода на Венере")).embedding
    assert sum(a * b for a, b in zip(doc, close, strict=True)) > 0.3
    assert sum(a * b for a, b in zip(doc, far, strict=True)) < 0.1


async def test_load_test_knobs_are_off_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    settings = LLMSettings()
    llm = build_llm_gateway(httpx.AsyncClient(), settings, asyncio.Semaphore(1))
    assert isinstance(llm, DevAdapter)
    assert llm.latency == 0
    assert llm._concurrency is None
    embedder = build_embedding_gateway(
        httpx.AsyncClient(), settings, query_throttle=_Busy()
    )
    await embedder.embed_query("вопрос")  # темп не применяется


async def test_fake_quotas_take_the_generation_slot_and_the_throttle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Нагрузочная проверка: поддельная модель занимает слот семафора на
    всё время ответа, эмбеддер ждёт темпа — как настоящие."""
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("LLM_FAKE_QUOTAS", "true")
    monkeypatch.setenv("LLM_FAKE_LATENCY_MS", "50")
    monkeypatch.setenv("LLM_FAKE_STREAM_DELAY_MS", "0")
    settings = LLMSettings()
    semaphore = asyncio.Semaphore(1)
    llm = build_llm_gateway(httpx.AsyncClient(), settings, semaphore)
    messages = [Message(role=Role.USER, content="вопрос")]

    started = time.monotonic()
    await asyncio.gather(llm.generate(messages), llm.generate(messages))
    # Слот один: второй ответ ждёт первого.
    assert time.monotonic() - started >= 0.1

    stream = llm.stream(messages)
    await anext(stream)
    assert semaphore.locked()
    async for _ in stream:
        pass
    assert not semaphore.locked()

    embedder = build_embedding_gateway(
        httpx.AsyncClient(), settings, query_throttle=_Busy()
    )
    with pytest.raises(ThrottleBusyError):
        await embedder.embed_query("вопрос")


class _Busy(Throttle):
    async def acquire(self) -> None:
        raise ThrottleBusyError(9.0)
