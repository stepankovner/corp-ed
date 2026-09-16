import re

PARAGRAPH_SEPARATOR = "\n\n"
SENTENCE_SEPARATOR = " "
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> list[str]:
    """Границы предложений"""
    return [s for s in _SENTENCE_BOUNDARY.split(text) if s.strip()]


def _pack(parts: list[str], limit: int, separator: str) -> list[str]:
    """Собрать куски в группы, не превышающие limit.

    Кусок, который сам длиннее limit, возвращается отдельной группой
    как есть: резать внутри куска — задача вызывающего.
    """
    result: list[str] = []
    buffer: list[str] = []

    for part in parts:
        if not buffer:
            buffer.append(part)
            continue

        if len(separator.join([*buffer, part])) <= limit:
            buffer.append(part)
        else:
            result.append(separator.join(buffer))
            buffer = [part]

    if buffer:
        result.append(separator.join(buffer))

    return result


def _apply_overlap(chunks: list[str], overlap: int) -> list[str]:
    """Добавить в начало каждого чанка хвост предыдущего.

    Хвост берётся из исходного соседа, а не из уже перекрытого, иначе
    перекрытия наслаивались бы и чанки росли к концу документа.
    Чанк становится длиннее chunk_size на overlap — это осознанно:
    chunk_size отвечает за объём нового текста, overlap за контекст слева.
    """
    if overlap <= 0 or len(chunks) < 2:
        return chunks

    result = [chunks[0]]
    for previous, current in zip(chunks, chunks[1:], strict=False):
        tail = previous[-overlap:]
        result.append(f"{tail} {current}")

    return result


def split_into_chunks(
    text: str,
    *,
    chunk_size: int,
    overlap: int,
) -> list[str]:
    paragraphs = [p.strip() for p in text.split(PARAGRAPH_SEPARATOR) if p.strip()]
    groups = _pack(paragraphs, chunk_size, PARAGRAPH_SEPARATOR)

    chunks: list[str] = []
    for group in groups:
        if len(group) <= chunk_size:
            chunks.append(group)
        else:
            sentences = _split_sentences(group)
            chunks.extend(_pack(sentences, chunk_size, SENTENCE_SEPARATOR))

    return _apply_overlap(chunks, overlap)
