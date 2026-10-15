"""Id документа в kronto: путь UNC в нижнем регистре, длинный — хеш."""

from datetime import UTC, datetime

from kronto_agent.smb import FileEntry
from kronto_agent.sync import MAX_EXTERNAL_ID, external_id


def entry(unc: str) -> FileEntry:
    return FileEntry(
        unc=unc,
        rel=unc.split("\\", 4)[-1],
        name=unc.rsplit("\\", 1)[-1],
        size=1,
        modified=datetime(2026, 10, 15, tzinfo=UTC),
        file_id=1,
    )


def test_short_path_is_lowercased_unc() -> None:
    assert external_id(entry("\\\\FS01\\Docs\\Кадры\\План.docx")) == (
        "\\\\fs01\\docs\\кадры\\план.docx"
    )


def test_long_path_becomes_stable_hash() -> None:
    deep = "\\\\fs01\\docs\\" + "\\".join(["папка"] * 120) + "\\файл.docx"
    first = external_id(entry(deep))
    assert len(deep) > MAX_EXTERNAL_ID
    assert first.startswith("sha256:") and len(first) <= MAX_EXTERNAL_ID
    assert external_id(entry(deep.upper())) == first
