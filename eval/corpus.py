"""Корпус для офлайн-стенда и генерации серебряного набора.

Папка с документами → Markdown → preprocess → чанки нужной конфигурации.
Извлечение повторяет то, что предложено бэкенду (docs/backend-handoff.md, BH-2):
docx — mammoth + markdownify, pdf — pymupdf4llm постранично через \\f.
md и txt читаются как есть. Библиотеки извлечения нужны, только если в
папке есть docx/pdf (eval/requirements.txt).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from corp_ed.domain.context import Section
from corp_ed.domain.split import (
    SectionDraft,
    format_breadcrumbs,
    split_into_chunks,
    split_sections,
)
from corp_ed.ingest.preprocess import PAGE_BREAK, preprocess

SUPPORTED_SUFFIXES = (".md", ".txt", ".docx", ".pdf")


@dataclass(frozen=True)
class Document:
    title: str
    markdown: str


@dataclass(frozen=True)
class BenchChunk:
    id: str
    material: str
    position: int
    heading_path: list[str]
    embed_text: str
    llm_text: str
    section_id: str = ""
    """Ключ секции в section_corpus (M2); у нарезки v1 секций нет."""


def section_key(title: str, position: int) -> str:
    return f"{title}#s{position}"


@dataclass(frozen=True)
class ChunkingConfig:
    """Конфигурация нарезки для экспериментов E1–E3."""

    version: Literal["v1", "v2"] = "v2"
    chunk_tokens: int = 400
    overlap_tokens: int = 50
    chunk_size: int = 1000
    overlap: int = 100
    crumbs_in_embed: bool = True
    title_in_crumbs: bool = True

    @property
    def name(self) -> str:
        if self.version == "v1":
            return f"v1-{self.chunk_size}-{self.overlap}"
        if not self.crumbs_in_embed:
            suffix = "-nocrumbs"
        elif not self.title_in_crumbs:
            suffix = "-notitle"
        else:
            suffix = ""
        return f"v2-{self.chunk_tokens}-{self.overlap_tokens}{suffix}"


def extract_markdown(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".md", ".txt"):
        return path.read_text(encoding="utf-8")
    if suffix == ".docx":
        import mammoth
        import markdownify

        with path.open("rb") as file:
            html = mammoth.convert_to_html(file).value
        return str(markdownify.markdownify(html, heading_style="ATX"))
    if suffix == ".pdf":
        import pymupdf4llm

        pages = pymupdf4llm.to_markdown(str(path), page_chunks=True)
        return PAGE_BREAK.join(str(page["text"]) for page in pages)
    raise ValueError(f"unsupported file type: {path.name}")


def load_corpus(directory: Path) -> list[Document]:
    paths = sorted(
        p for p in directory.iterdir() if p.suffix.lower() in SUPPORTED_SUFFIXES
    )
    if not paths:
        raise SystemExit(f"В {directory} нет файлов {SUPPORTED_SUFFIXES}")
    return [
        Document(title=p.stem, markdown=preprocess(extract_markdown(p))) for p in paths
    ]


def chunk_corpus(documents: list[Document], config: ChunkingConfig) -> list[BenchChunk]:
    """Нарезать все документы. Для v1 embed_text = llm_text = текст чанка."""
    chunks: list[BenchChunk] = []
    for document in documents:
        if config.version == "v1":
            texts = split_into_chunks(
                document.markdown, chunk_size=config.chunk_size, overlap=config.overlap
            )
            chunks.extend(
                BenchChunk(
                    id=f"{document.title}#{position}",
                    material=document.title,
                    position=position,
                    heading_path=[],
                    embed_text=text,
                    llm_text=text,
                )
                for position, text in enumerate(texts)
            )
            continue

        for section in _sections(document, config):
            for draft in section.chunks:
                embed_text = draft.embed_text
                if not config.crumbs_in_embed:
                    embed_text = _replace_crumbs(
                        embed_text, document.title, draft.heading_path, ""
                    )
                elif not config.title_in_crumbs:
                    embed_text = _replace_crumbs(
                        embed_text,
                        document.title,
                        draft.heading_path,
                        format_breadcrumbs("", draft.heading_path),
                    )
                chunks.append(
                    BenchChunk(
                        id=f"{document.title}#{draft.position}",
                        material=document.title,
                        position=draft.position,
                        heading_path=draft.heading_path,
                        embed_text=embed_text,
                        llm_text=draft.llm_text,
                        section_id=section_key(document.title, section.position),
                    )
                )
    return chunks


def section_corpus(
    documents: list[Document], config: ChunkingConfig
) -> dict[str, Section]:
    """Секции по section_id для select_sections (M2): текст целиком и чанки
    по порядку. Та же нарезка, что в chunk_corpus, поэтому llm_text чанков
    совпадает с BenchChunk.llm_text — по нему select_sections находит чанк
    в секции. У нарезки v1 секций нет."""
    if config.version == "v1":
        return {}
    return {
        section_key(document.title, section.position): Section(
            content=section.llm_text,
            chunks=[chunk.llm_text for chunk in section.chunks],
        )
        for document in documents
        for section in _sections(document, config)
    }


def _sections(document: Document, config: ChunkingConfig) -> list[SectionDraft]:
    return split_sections(
        document.markdown,
        title=document.title,
        chunk_tokens=config.chunk_tokens,
        overlap_tokens=config.overlap_tokens,
    )


def _replace_crumbs(
    embed_text: str, title: str, heading_path: list[str], new_crumbs: str
) -> str:
    """E2: тот же чанк, но первая строка-крошки заменена (или убрана).

    new_crumbs = "" — без крошек. Крошки без названия документа отделяют
    вклад заголовков разделов от вклада названия: у демо-корпуса название —
    имя файла вида «Pravila-otbora_Akselerator-…».
    """
    crumbs = format_breadcrumbs(title, heading_path)
    prefix = f"{crumbs}\n"
    if not crumbs or not embed_text.startswith(prefix):
        return embed_text
    body = embed_text[len(prefix) :]
    return f"{new_crumbs}\n{body}" if new_crumbs else body
