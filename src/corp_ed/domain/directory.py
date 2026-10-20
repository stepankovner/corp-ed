"""Справочник «должность → человек, кабинет, телефон, почта» — поиск кодом.

Попытки 2–3 (ml-report.md): на вопросах «из двух документов» шаг решения
верно просит добор («финансовый директор ФИО e-mail»), но векторный поиск
не приносит нужную строку справочника оргструктуры, а добор удлиняет
цепочку вызовов — главный источник p95. Практика (ml-agent-plan.md §9):
структурированные данные — таблицей или инструментом, не чанками.

Здесь — чистые функции: разбор справочника из Markdown (строки вида
«— Должность — Имя Отчество Фамилия, кабинет N, доб. N, почта») и поиск
по должности или подразделению. Ничего не угадывает: нет совпадения —
пустой список, дальше обычный поиск.
"""

import re
from dataclasses import dataclass

_LINE = re.compile(r"^\s*[—–-]\s+(?P<left>[^—–]+?)\s+[—–]\s+(?P<right>.+?)\s*\.?\s*$")
_NAME = re.compile(
    r"\b([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)\b"
)
_ROOM = re.compile(r"кабинет\s+([\w\-]+)", re.IGNORECASE)
_PHONE = re.compile(r"(?:доб\.?|добавочный|тел\.?|телефон)\s*([\d\-]+)", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_PAREN = re.compile(r"\(([^)]+)\)")
_STOP = {
    "фио",
    "имя",
    "фамилия",
    "отчество",
    "кабинет",
    "телефон",
    "доб",
    "добавочный",
    "номер",
    "почта",
    "email",
    "e-mail",
    "mail",
    "контакт",
    "контакты",
    "кто",
    "как",
    "зовут",
    "чей",
    "чьё",
    "чье",
    "кого",
    "компании",
    "компания",
    "и",
    "или",
    "в",
    "у",
}


@dataclass(frozen=True)
class Entry:
    role: str
    """Должность или роль, как в справочнике («Финансовый директор»,
    «руководитель»)."""
    unit: str
    """Подразделение, если строка про него («Бухгалтерия», «ЦУП»); иначе пусто."""
    name: str
    room: str = ""
    phone: str = ""
    email: str = ""

    def text(self) -> str:
        who = " — ".join(part for part in (self.unit, self.role) if part)
        tail = ", ".join(
            part
            for part in (
                self.name,
                f"кабинет {self.room}" if self.room else "",
                f"доб. {self.phone}" if self.phone else "",
                self.email,
            )
            if part
        )
        return f"{who} — {tail}" if tail else who


def parse_directory(text: str) -> list[Entry]:
    """Записи из строк справочника; строки без должности или подразделения
    пропускаются."""
    entries = []
    for line in text.splitlines():
        match = _LINE.match(line)
        if not match:
            continue
        left, right = match.group("left").strip(), match.group("right").strip()
        name_match = _NAME.search(right)
        name = name_match.group(1) if name_match else ""
        room = _ROOM.search(right)
        phone = _PHONE.search(right)
        email = _EMAIL.search(right)
        # «— Бухгалтерия — главный бухгалтер Имя…»: слева подразделение, роль —
        # слова справа до имени; «— Финансовый директор — Имя…»: слева роль.
        before_name = right[: name_match.start()].strip(" ,") if name_match else ""
        before_name = _ROOM.sub("", before_name).strip(" ,")
        if before_name:
            role, unit = before_name, left
        else:
            role, unit = left, ""
        entries.append(
            Entry(
                role=role,
                unit=unit,
                name=name,
                room=room.group(1) if room else "",
                phone=phone.group(1) if phone else "",
                email=email.group(0) if email else "",
            )
        )
    return entries


def _stem(word: str) -> str:
    word = word.casefold().replace("ё", "е")
    return word[:5] if len(word) > 5 else word


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[\w-]+", text.casefold().replace("ё", "е"))
    out = set()
    for word in words:
        if word in _STOP or len(word) <= 2 or word.isdigit():  # предлоги, числа
            continue
        out.add(_stem(word))
    return out


def _entry_tokens(entry: Entry) -> set[str]:
    tokens = _tokens(entry.role) | _tokens(entry.unit)
    for abbr in _PAREN.findall(entry.unit):
        tokens.add(_stem(abbr))
    return tokens


def lookup(entries: list[Entry], query: str) -> list[Entry]:
    """Записи, у которых должность или подразделение покрывают запрос.

    Запрос — как его пишет шаг решения: «финансовый директор ФИО»,
    «руководитель ОКП кабинет». Совпадение — по основам слов (первые 5
    букв), служебные слова («ФИО», «кабинет») не считаются. Берутся записи,
    покрывающие все содержательные слова запроса; если таких нет — ничего.
    """
    wanted = _tokens(query)
    if not wanted:
        return []
    scored = []
    for entry in entries:
        have = _entry_tokens(entry)
        hit = len(wanted & have)
        if hit == len(wanted):
            scored.append((hit, -len(have), entry))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [entry for _, _, entry in scored]


def format_entries(entries: list[Entry]) -> str:
    return "\n".join(f"— {entry.text()}." for entry in entries)


# Какие поля записи нужны по словам «чего не хватает» из решения.
_FIELD_WORDS = (
    ("name", re.compile(r"фио|имя|имени|фамили|отчеств|зовут|кто\b", re.IGNORECASE)),
    ("room", re.compile(r"кабинет|комнат", re.IGNORECASE)),
    ("phone", re.compile(r"телефон|добавочн|\bдоб\b|мобильн", re.IGNORECASE)),
    ("email", re.compile(r"e-?mail|mail|почт|адрес\s+эл|эл\.\s*адрес", re.IGNORECASE)),
)
_HYPHENS = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-"})


def requested_fields(missing: str) -> frozenset[str]:
    """Поля записи, о которых говорит «чего не хватает» («ФИО и кабинет» →
    имя и кабинет; «email-адрес отдела персонала» → почта). Пусто —
    ничего конкретного не названо."""
    text = missing.translate(_HYPHENS)
    return frozenset(field for field, pattern in _FIELD_WORDS if pattern.search(text))


def covers(entries: list[Entry], fields: frozenset[str]) -> bool:
    """Есть ли запись, у которой заполнены все запрошенные поля.

    Попадание по роли без нужного поля (у «Отдела персонала» в справочнике
    нет почты) — не повод отвечать сразу: дальше обычный векторный добор.
    """
    if not entries:
        return False
    return any(all(getattr(entry, field) for field in fields) for entry in entries)
