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

Замещение (ml-agent-plan.md §10.4, фаза 0, 21.10). Вопросы «руководитель
ЦУП в отпуске, заместитель не назначен — кто согласует?» (T4-04, T5-19,
T3-05) отвечаются не строкой справочника, а цепочкой из двух абзацев
оргструктуры: «Замещение» даёт правило («если заместитель не назначен,
вопросы решает вышестоящий руководитель»), «Подчинённость» — кто
вышестоящий («Финансовому директору подчиняются бухгалтерия … и ЦУП»).
Пар «X замещает Y» в оргструктуре стенда нет. Поэтому замещающий — поле
той же записи (`Entry.deputy`, одна таблица для бэкенда): явная фраза
«X-а при отсутствии замещает Y» или — только если в документе есть
правило о вышестоящем — вышестоящий по «Подчинённости». Основание
словами документа — `Entry.deputy_note`. Поиск — отдельная функция
`lookup_substitute` (пары `Substitute`: отсутствующий + замещающий);
`lookup` не меняется. Когда звать её — решает вызывающий по тексту
роли, «чего не хватает» и вопроса (`substitution_requested`).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

DIRECTORY_HEADING = "Справочник"
"""Заголовок выдержки с записями справочника (BH-47)."""
DIRECTORY_HEADING_EXPLAINED = "Справочник: должность → человек, кабинет, телефон, почта"
"""Вариант A1 шага ответа (ml-report, «Шаг ответа после справочника»):
заголовок, который прямо говорит модели, что в выдержке — контакты по
должностям."""

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
    deputy: str = ""
    """Кто решает вопросы при отсутствии — `who()` другой записи
    («Финансовый директор»). Пусто — документ этого не говорит."""
    deputy_note: str = ""
    """Основание словами документа, если замещающий — вышестоящий по
    правилу («если заместитель не назначен, вопросы решает вышестоящий
    руководитель»); при явной фразе «X замещает Y» — пусто."""

    def who(self) -> str:
        """«Подразделение — должность» или одна должность: так запись
        называет `deputy` другой записи."""
        return " — ".join(part for part in (self.unit, self.role) if part)

    def text(self) -> str:
        who = self.who()
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
    пропускаются. Замещающие (`deputy`) — из абзацев «Замещение» и
    «Подчинённость» (`_link_deputies`)."""
    entries: list[Entry] = []
    bosses: dict[int, str] = {}
    prose: list[str] = []
    for line in text.splitlines():
        match = _LINE.match(line)
        if not match:
            prose.append(line)
            continue
        boss = _BOSS_IN_LINE.search(match.group("right"))
        if boss:  # «— Отдел персонала — подчиняется директору по персоналу, …»
            bosses[len(entries)] = boss.group("boss")
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
    return _link_deputies(entries, prose, bosses)


# Замещение и подчинённость — предложения обычного текста (не строки с тире).
# Точка после «доб.» и «тел.» — не конец предложения.
_SENTENCE = re.compile(r"(?<![Дд]об\.)(?<![Тт]ел\.)(?<=[.!?])\s+")
_BOSS_IN_LINE = re.compile(r"подчиняется\s+(?P<boss>[^,.]+)", re.IGNORECASE)
_SUBORDINATION = re.compile(
    r"^(?P<left>.+?)\s+(?:подчиняются|подчиняется|подотч[её]тн[аы]?|подотч[её]тен)"
    r"\s+(?P<right>.+)$",
    re.IGNORECASE,
)
_FILLER = re.compile(r"\b(?:независим\w*|напрямую|непосредственно)\b", re.IGNORECASE)
_LIST_SPLIT = re.compile(r"\s*,\s*|\s+и\s+")
_ABSENCE = (
    r"(?:на время отсутствия|во время отсутствия|в период отсутствия"
    r"|при (?:его |её |ее )?отсутствии|в (?:его |её |ее )?отсутствие)"
)
# «На время отсутствия финансового директора его обязанности исполняет …»
_EXPLICIT_LEAD = re.compile(
    rf"^{_ABSENCE}\s+(?P<absent>.+?)\s+(?:(?:его|её|ее)\s+)?(?:обязанности\s+)?"
    r"(?:исполняет|замещает)\s+(?P<deputy>.+)$",
    re.IGNORECASE,
)
# «Главного бухгалтера при отсутствии замещает …»
_EXPLICIT_TAIL = re.compile(
    rf"^(?P<absent>.+?)\s+{_ABSENCE}\s+(?:(?:его|её|ее)\s+)?замещает\s+(?P<deputy>.+)$",
    re.IGNORECASE,
)
_DATIVE = re.compile(r"^[\w-]*(?:ому|ему|у|ю)\b", re.IGNORECASE)
_RULE = re.compile(r"вышестоящ", re.IGNORECASE)
_RULE_CONTEXT = re.compile(r"замест|отсутств", re.IGNORECASE)


def _sentences(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        for sentence in _SENTENCE.split(line.strip()):
            sentence = sentence.strip().rstrip(".").strip()
            if sentence:
                out.append(sentence)
    return out


def _resolve(entries: list[Entry], text: str) -> int | None:
    """Номер единственной записи, которую называет текст («Финансовому
    директору», «ЦУП», «Главного бухгалтера»); несколько или ни одной —
    None (не угадываем)."""
    text = text.strip(" ,")
    if not text:
        return None
    key = _normalize(text)
    exact = [
        i
        for i, entry in enumerate(entries)
        if key in {_normalize(part) for part in (entry.who(), entry.role, entry.name)}
    ]
    if len(exact) == 1:
        return exact[0]
    wanted = _tokens(text)
    if not wanted:
        return None
    found = [i for i, entry in enumerate(entries) if wanted <= _entry_tokens(entry)]
    return found[0] if len(found) == 1 else None


def _deputy_entry(entries: list[Entry], text: str) -> int | None:
    """Замещающий из явной фразы: запись по имени или по должности; если
    в фразе есть ФИО, а записи нет — новая запись (должность — слова до
    имени)."""
    name = _NAME.search(text)
    if not name:
        return _resolve(entries, text)
    for i, entry in enumerate(entries):
        if entry.name == name.group(1):
            return i
    role = _ROOM.sub("", text[: name.start()]).strip(" ,")
    if not role:
        return None
    room, phone, email = _ROOM.search(text), _PHONE.search(text), _EMAIL.search(text)
    entries.append(
        Entry(
            role=role,
            unit="",
            name=name.group(1),
            room=room.group(1) if room else "",
            phone=phone.group(1) if phone else "",
            email=email.group(0) if email else "",
        )
    )
    return len(entries) - 1


def _subordinates(entries: list[Entry], text: str, boss: int) -> list[int]:
    out = []
    for item in _LIST_SPLIT.split(_FILLER.sub("", text)):
        index = _resolve(entries, item) if item.strip() else None
        if index is not None and index != boss:
            out.append(index)
    return out


def _link_deputies(
    entries: list[Entry], prose: list[str], bosses: dict[int, str]
) -> list[Entry]:
    """Поля deputy / deputy_note по тексту документа.

    1. Явные пары («На время отсутствия X его обязанности исполняет Y»,
       «X-а при отсутствии замещает Y») — если X — ровно одна запись, а Y —
       ровно одна запись или фраза с ФИО. Общее правило стенда («На время
       отсутствия руководителя … исполняет заместитель, назначенный
       приказом») пары не даёт: «руководитель» — две записи, у
       «заместителя» нет ни записи, ни имени.
    2. Вышестоящий — из «Подчинённости» («Финансовому директору
       подчиняются бухгалтерия, казначейство и ЦУП», «ИТ-директор и
       директор по персоналу подчиняются генеральному директору»,
       «… подотчётен генеральному директору») и из строки записи
       («подчиняется директору по персоналу»). Замещающим он становится,
       только если в тексте есть правило о вышестоящем при отсутствии
       заместителя; основание — это предложение.
    """
    entries = list(entries)
    sentences = _sentences(prose)
    explicit: dict[int, int] = {}
    for sentence in sentences:
        match = _EXPLICIT_LEAD.match(sentence) or _EXPLICIT_TAIL.match(sentence)
        if not match:
            continue
        absent = _resolve(entries, match.group("absent"))
        if absent is None:
            continue
        deputy = _deputy_entry(entries, match.group("deputy"))
        if deputy is not None and deputy != absent:
            explicit[absent] = deputy

    superior: dict[int, int] = {}
    for index, text in bosses.items():
        boss = _resolve(entries, text)
        if boss is not None and boss != index:
            superior[index] = boss
    for sentence in sentences:
        match = _SUBORDINATION.match(sentence)
        if not match:
            continue
        left = _FILLER.sub("", match.group("left")).strip(" ,")
        left = re.sub(r"\s+и$", "", left)
        right = _FILLER.sub("", match.group("right")).strip(" ,")
        # Вышестоящий — в дательном падеже: «Финансовому директору
        # подчиняются …», «… подчиняются генеральному директору».
        if _DATIVE.match(left) and not _DATIVE.match(right):
            boss_text, subs_text = left, right
        elif _DATIVE.match(right):
            boss_text, subs_text = right, left
        else:
            continue
        boss = _resolve(entries, boss_text)
        if boss is None:
            continue
        for sub in _subordinates(entries, subs_text, boss):
            superior.setdefault(sub, boss)

    rule = next(
        (s for s in sentences if _RULE.search(s) and _RULE_CONTEXT.search(s)), ""
    )
    note = rule[:1].lower() + rule[1:]
    for absent, deputy in explicit.items():
        entries[absent] = replace(entries[absent], deputy=entries[deputy].who())
    if note:
        for index, boss in superior.items():
            if index not in explicit:
                entries[index] = replace(
                    entries[index], deputy=entries[boss].who(), deputy_note=note
                )
    return entries


def _stem(word: str) -> str:
    word = word.casefold().replace("ё", "е")
    return word[:5] if len(word) > 5 else word


# Дефисы, которые модели ставят вместо обычного («ИТ‑директор» с U+2011), и
# латинское «IT» — к написанию справочника (приёмка 4, T4-09: «IT‑директор»
# распадался на «директор» и находил четырёх других директоров).
_DASHES = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2212"})
_LATIN_IT = re.compile(r"\bit(?=-|\b)")


def _normalize(text: str) -> str:
    text = text.casefold().replace("ё", "е").translate(_DASHES)
    return _LATIN_IT.sub("ит", text)


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[\w-]+", _normalize(text))
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
    return _match(entries, _tokens(query))


def _match(entries: Sequence[Entry], wanted: set[str]) -> list[Entry]:
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


@dataclass(frozen=True)
class Substitute:
    """Пара «отсутствующий → кто решает вопросы вместо него».

    Для `format_entries`, `covers` и `contact_note` ведёт себя как запись:
    `text()` — обе записи со связью, поля (`name`, `room`, `phone`,
    `email`) — замещающего: спрашивают про того, к кому идти."""

    absent: Entry
    deputy: Entry

    @property
    def name(self) -> str:
        return self.deputy.name

    @property
    def room(self) -> str:
        return self.deputy.room

    @property
    def phone(self) -> str:
        return self.deputy.phone

    @property
    def email(self) -> str:
        return self.deputy.email

    def text(self) -> str:
        """«Отсутствующий; при отсутствии: <правило> — замещающий» —
        основание словами документа; явная пара — «при отсутствии
        замещает»."""
        note = self.absent.deputy_note
        link = f"при отсутствии: {note}" if note else "при отсутствии замещает"
        return f"{self.absent.text()}; {link} — {self.deputy.text()}"


# Слова о замещении: по ним вызывающий решает звать lookup_substitute
# (роль, «чего не хватает», вопрос). «В командировке» не берём: в T5-19 в
# командировке был сам спрашивающий. «Отсутствие» — только существительное:
# «отсутствует кабинет главного бухгалтера» в «чего не хватает» — не про
# замещение.
_SUBSTITUTION = re.compile(
    r"замещ|заместител|отсутстви|\bвместо\b|в отпуске|на больничном|\bболеет\b"
    r"|вышестоящ|исполня\w* обязанност|\bврио\b"
)
# Основы слов о замещении — из роли их убираем, остаётся, кого замещают
# («замещающий главного бухгалтера» → «главного бухгалтера»).
_SUBSTITUTION_STEMS = frozenset(
    {
        "замещ",
        "замес",
        "отсут",
        "вмест",
        "испол",
        "обяза",
        "вышес",
        "отпус",
        "назна",
        "прика",
        "время",
        "перио",
        "при",
        "его",
    }
)
_HEAD = "руков"
"""«Вышестоящий руководитель главного бухгалтера»: без «руководитель» —
запись главного бухгалтера; пробуем, только если с ним записи нет."""


def substitution_requested(text: str) -> bool:
    """Говорит ли текст (роль, «чего не хватает», вопрос) об отсутствии
    или замещении: «в отпуске», «заместитель не назначен», «кто замещает»."""
    return bool(_SUBSTITUTION.search(_normalize(text)))


def lookup_substitute(entries: Sequence[Entry], role: str) -> list[Substitute]:
    """Пары «отсутствующий → замещающий» по роли из решения.

    Роль — как пишет шаг решения: «руководитель ЦУП», «заместитель главного
    бухгалтера», «кто замещает финансового директора». Сначала роль как
    есть (если в справочнике есть сама запись замещающего — пара, где она
    замещающий), потом без слов о замещении. Пара — только если у записи
    известен замещающий (`Entry.deputy`) и он есть в справочнике; иначе
    пусто — вызывающий идёт обычным `lookup`.
    """
    wanted = _tokens(role)
    pairs = _pairs(entries, _match(entries, wanted))
    if pairs:
        return pairs
    core = {token for token in wanted if token not in _SUBSTITUTION_STEMS}
    if not core or core == wanted:
        return []
    found = _match(entries, core)
    if not found and _HEAD in core and len(core) > 1:
        found = _match(entries, core - {_HEAD})
    return _pairs(entries, found)


def _pairs(entries: Sequence[Entry], found: list[Entry]) -> list[Substitute]:
    by_who = {entry.who(): entry for entry in entries}
    out: list[Substitute] = []
    for entry in found:
        if entry.deputy and entry.deputy in by_who:
            pair = Substitute(absent=entry, deputy=by_who[entry.deputy])
            if pair not in out:
                out.append(pair)
            continue
        # Найдена сама запись замещающего («заместитель главного бухгалтера»).
        for absent in entries:
            if absent.deputy == entry.who() and not absent.deputy_note:
                pair = Substitute(absent=absent, deputy=entry)
                if pair not in out:
                    out.append(pair)
    return out


def format_entries(entries: Sequence[Entry | Substitute]) -> str:
    return "\n".join(f"— {entry.text()}." for entry in entries)


# Какие поля записи нужны по словам «чего не хватает» из решения. «Адрес»
# без уточнения — почта: других адресов в справочнике нет, а «адрес отдела»
# (этап выбора, T2-11) — это адрес почты.
_FIELD_WORDS = (
    ("name", re.compile(r"фио|имя|имени|фамили|отчеств|зовут|кто\b", re.IGNORECASE)),
    ("room", re.compile(r"кабинет|комнат", re.IGNORECASE)),
    ("phone", re.compile(r"телефон|добавочн|\bдоб\b|мобильн", re.IGNORECASE)),
    ("email", re.compile(r"e-?mail|mail|почт|адрес", re.IGNORECASE)),
)
_HYPHENS = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-"})


def requested_fields(missing: str) -> frozenset[str]:
    """Поля записи, о которых говорит «чего не хватает» («ФИО и кабинет» →
    имя и кабинет; «email-адрес отдела персонала» → почта). Пусто —
    ничего конкретного не названо."""
    text = missing.translate(_HYPHENS)
    return frozenset(field for field, pattern in _FIELD_WORDS if pattern.search(text))


def covers(entries: Sequence[Entry | Substitute], fields: frozenset[str]) -> bool:
    """Есть ли запись, у которой заполнены все запрошенные поля.

    Попадание по роли без нужного поля (у «Отдела персонала» в справочнике
    нет почты) — не повод отвечать сразу: дальше обычный векторный добор.
    У пары `Substitute` поля — замещающего: почта отсутствующего не в счёт.
    """
    if not entries:
        return False
    return any(all(getattr(entry, field) for field in fields) for entry in entries)
