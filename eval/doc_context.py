"""Контекст документа для стенда (§9.3 п. 4): извлечение с кэшем и флаги.

Один вызов модели на документ (doc-context-v2): имя файла + начало
документа → карточка `DocContext` → строка контекста (`context_phrase`);
статус редакции — правилом по корпусу (`edition_statuses`). Кэш — JSON
`eval/.cache/doc_context.json`, ключ = версия промпта | модель | имя
файла | хэш начала документа: повторный прогон бесплатный, правка
документа или промпта — новый вызов.

Флаги — общие для bench и offline_e2e:
    --doc-context embed   строка контекста в embed_text (поиск)
    --doc-context llm     только в llm_text (её видят решение и ответ)
    --doc-context both    в оба

Таблица карточек корпуса (для отчёта и просмотра глазами):
    python -m eval.doc_context --corpus ../sandbox/stand/corpus_product
"""

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from corp_ed.domain.doc_context import (
    DocContext,
    EditionStatus,
    context_phrase,
    document_head,
    edition_statuses,
    parse_doc_context,
)
from corp_ed.domain.tokens import count_tokens
from corp_ed.llm.types import Message
from corp_ed.prompts.doc_context import (
    DOC_CONTEXT_PROMPT_VERSION,
    build_doc_context_messages,
    doc_context_response_format,
)
from eval.corpus import Document, load_corpus
from eval.yandex import DEFAULT_LLM, Completion

DEFAULT_CACHE = Path("eval/.cache/doc_context.json")
DEFAULT_PROVIDER = "yandex"
"""Flash у Яндекса — та же модель, что у бэкенда: карточки, как в продукте."""


class _Completer(Protocol):
    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = ...,
        max_tokens: int = ...,
        api: Any = ...,
        response_format: dict[str, Any] | None = ...,
        extra: dict[str, Any] | None = ...,
    ) -> Completion: ...


@dataclass(frozen=True)
class Extracted:
    """Карточка документа и цена её извлечения (нули — из кэша)."""

    context: DocContext | None
    input_tokens: int = 0
    output_tokens: int = 0
    cached: bool = False


def cache_key(model: str, file_name: str, head: str) -> str:
    digest = hashlib.sha256(head.encode("utf-8")).hexdigest()[:16]
    return f"{DOC_CONTEXT_PROMPT_VERSION}|{model}|{file_name}|{digest}"


def extract_contexts(
    client: _Completer,
    documents: Sequence[Document],
    *,
    model: str = DEFAULT_LLM,
    provider: str = DEFAULT_PROVIDER,
    cache_path: Path = DEFAULT_CACHE,
    extra: dict[str, Any] | None = None,
    max_tokens: int = 600,
) -> dict[str, Extracted]:
    """Карточки по названию документа. Не по схеме — None (контекста нет)."""
    cache: dict[str, dict[str, Any]] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    dirty = False
    results: dict[str, Extracted] = {}
    for document in documents:
        head = document_head(document.markdown)
        key = cache_key(f"{provider}:{model}", document.title, head)
        if key in cache:
            entry = cache[key]
            results[document.title] = Extracted(
                DocContext.model_validate(entry["context"])
                if entry["context"]
                else None,
                int(entry.get("input_tokens", 0)),
                int(entry.get("output_tokens", 0)),
                cached=True,
            )
            continue
        completion = client.complete(
            build_doc_context_messages(document.title, head),
            model=model,
            temperature=0.0,
            max_tokens=max_tokens,
            api="openai",
            response_format=doc_context_response_format(),
            extra=extra,
        )
        context = parse_doc_context(completion.text)
        if context is None:
            print(
                f"doc-context: «{document.title}» — ответ не по схеме: "
                f"{completion.text[:200]!r}",
                file=sys.stderr,
            )
        cache[key] = {
            "context": context.model_dump() if context else None,
            "input_tokens": completion.input_tokens,
            "output_tokens": completion.output_tokens,
        }
        dirty = True
        results[document.title] = Extracted(
            context, completion.input_tokens, completion.output_tokens
        )
    if dirty:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    return results


def corpus_statuses(documents: Sequence[Document]) -> dict[str, EditionStatus]:
    """Статус редакции по названиям и текстам корпуса (правило BH-41)."""
    return edition_statuses({d.title: (d.title, d.markdown) for d in documents})


def attach_contexts(
    documents: Sequence[Document],
    extracted: dict[str, Extracted],
    statuses: dict[str, EditionStatus],
) -> list[Document]:
    """Документы со строкой контекста. Без карточки — только статус, если он есть."""
    attached = []
    for document in documents:
        found = extracted.get(document.title)
        status = statuses.get(document.title)
        if found is not None and found.context is not None:
            phrase = context_phrase(found.context, status)
        elif status is not None:
            phrase = context_phrase(
                DocContext(
                    title="", organization="", program="", track="", year="", edition=""
                ),
                status,
            )
        else:
            phrase = ""
        attached.append(replace(document, context=phrase))
    return attached


def add_doc_context_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--doc-context",
        choices=("off", "embed", "llm", "both"),
        default="off",
        help="контекст документа в чанке (§9.3 п. 4): embed — в эмбеддинг, "
        "llm — в текст для модели, both — в оба",
    )
    parser.add_argument(
        "--doc-context-provider",
        choices=("yandex", "cloudru", "mws"),
        default=DEFAULT_PROVIDER,
        help="кто извлекает карточку документа",
    )
    parser.add_argument(
        "--doc-context-model", default=DEFAULT_LLM, help="модель карточки документа"
    )
    parser.add_argument(
        "--doc-context-extra",
        default="",
        help='JSON полей запроса карточки, например {"reasoning_effort": "low"}',
    )


def make_client(provider: str) -> _Completer:
    if provider == "yandex":
        from eval.yandex import YandexClient

        return YandexClient.from_env()
    from eval.providers import OpenAICompatibleClient

    return OpenAICompatibleClient.from_env(provider)


def with_doc_context(
    args: argparse.Namespace, documents: list[Document]
) -> list[Document]:
    """Флаги → документы со строкой контекста (или как есть при off)."""
    if args.doc_context == "off":
        return documents
    extracted = extract_contexts(
        make_client(args.doc_context_provider),
        documents,
        model=args.doc_context_model,
        provider=args.doc_context_provider,
        extra=json.loads(args.doc_context_extra) if args.doc_context_extra else None,
    )
    attached = attach_contexts(documents, extracted, corpus_statuses(documents))
    paid = [e for e in extracted.values() if not e.cached]
    phrases = [count_tokens(d.context) for d in attached if d.context]
    print(
        f"Контекст документов ({DOC_CONTEXT_PROMPT_VERSION}, "
        f"{args.doc_context_provider}:{args.doc_context_model}): "
        f"{len(phrases)} из {len(documents)}, строка ≈ "
        f"{sum(phrases) // max(1, len(phrases))} ток.; новых вызовов {len(paid)}, "
        f"токенов {sum(e.input_tokens for e in paid)} / "
        f"{sum(e.output_tokens for e in paid)}"
    )
    return attached


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.doc_context",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--corpus", type=Path, required=True)
    add_doc_context_arguments(parser)
    args = parser.parse_args(argv)
    documents = load_corpus(args.corpus)
    extracted = extract_contexts(
        make_client(args.doc_context_provider),
        documents,
        model=args.doc_context_model,
        provider=args.doc_context_provider,
        extra=json.loads(args.doc_context_extra) if args.doc_context_extra else None,
    )
    attached = attach_contexts(documents, extracted, corpus_statuses(documents))
    print("| Документ | Строка контекста | Токенов карточки (вход / выход) |")
    print("|---|---|---|")
    for document in attached:
        found = extracted[document.title]
        print(
            f"| {document.title} | {document.context or '—'} | "
            f"{found.input_tokens} / {found.output_tokens} |"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
