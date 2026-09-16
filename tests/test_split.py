from corp_ed.domain.split import (
    _pack,
    _split_sentences,
    split_into_chunks,
)


def test_split_into_chunks_empty_text() -> None:
    result = split_into_chunks("", chunk_size=20, overlap=5)

    assert result == []


def test_split_into_chunks_whitespace_only() -> None:
    result = split_into_chunks(" \n\n", chunk_size=20, overlap=5)

    assert result == []


def test_split_into_chunks_text_shorter_than_limit() -> None:
    text = "Привет."
    result = split_into_chunks(text, chunk_size=50, overlap=5)

    assert result == [text]


def test_split_into_chunks_paragraphs_fit_together() -> None:
    text = "Привет.\n\nПока."
    result = split_into_chunks(text, chunk_size=20, overlap=0)

    assert result == [text]


def test_split_into_chunks_paragraphs_do_not_fit() -> None:
    text = "Привет.\n\nПока."
    result = split_into_chunks(text, chunk_size=8, overlap=0)

    assert result == ["Привет.", "Пока."]


def test_split_into_chunks_long_paragraph_fits_limit() -> None:
    text = "Первое предложение. Второе предложение. Третье предложение."
    chunk_size = 22
    result = split_into_chunks(text, chunk_size=chunk_size, overlap=0)

    assert len(result) == 3
    assert all(len(c) <= chunk_size for c in result)


def test_split_into_chunks_zero_overlap_adds_nothing() -> None:
    text = "Первое предложение. Второе предложение того же абзаца. Третье предложение."
    result = split_into_chunks(text, chunk_size=22, overlap=0)

    assert result == [
        "Первое предложение.",
        "Второе предложение того же абзаца.",
        "Третье предложение.",
    ]


def test_split_into_chunks_overlap_prepends_tail() -> None:
    text = "Первое предложение. Второе предложение того же абзаца."
    overlap = 5
    result = split_into_chunks(text, chunk_size=22, overlap=overlap)

    assert len(result) == 2
    assert result[0] == "Первое предложение."
    assert result[1][:overlap] == result[0][-overlap:]


def test_split_into_chunks_order_preserved() -> None:
    text = "Первый.\n\nВторой.\n\nТретий."
    result = split_into_chunks(text, chunk_size=8, overlap=0)

    assert result == ["Первый.", "Второй.", "Третий."]


def test_split_into_chunks_drops_empty_paragraphs() -> None:
    text = "Первый.\n\n\n\n\n\nВторой.\n\n"
    result = split_into_chunks(text, chunk_size=8, overlap=0)

    assert len(result) == 2
    assert all(c.strip() for c in result)
    assert all(c == c.strip() for c in result)


def test_pack_empty_list() -> None:
    result = _pack([], limit=10, separator=" ")

    assert result == []


def test_pack_keeps_part_longer_than_limit() -> None:
    # Намеренное поведение: _pack не режет внутри куска и отдаёт его целиком.
    parts = ["ааааааааа"]
    result = _pack(parts, limit=3, separator=" ")

    assert result == parts


def test_pack_counts_separators_in_length() -> None:
    # Сумма длин 10 влезает в 11, но со склейкой выходит 12.
    result = _pack(["aaaaa", "bbbbb"], limit=11, separator="  ")

    assert result == ["aaaaa", "bbbbb"]


def test_pack_fits_exactly_at_limit() -> None:
    result = _pack(["aaaaa", "bbbbb"], limit=12, separator="  ")

    assert result == ["aaaaa  bbbbb"]


def test_split_sentences_breaks_on_abbreviations() -> None:
    # Фиксирует текущее наивное поведение, а не правильное: регулярка
    # принимает точку в «ст.» за конец предложения. Тест должен упасть,
    # когда границы начнёт искать razdel.
    result = _split_sentences("Согласно ст. 7 закона. Далее текст.")

    assert result == ["Согласно ст.", "7 закона.", "Далее текст."]
