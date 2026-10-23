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

Отдел спрашивающего (фаза 0, 23.10). Общая роль из решения
(«руководитель», «непосредственный руководитель») при отделе,
названном в вопросе («Я в ОКП», «выхожу в бухгалтерию»), — глава этого
подразделения (`head_for_role`: `is_generic_role`, `asker_unit`,
`role_unit`, `unit_head`), как бы ни называлась его должность
(офис-менеджер, главный бухгалтер). Общая роль без отдела — промах
(`generic_role_miss`, решение Артёма 23.10). `lookup` не меняется.
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


# Отдел спрашивающего (фаза 0, 23.10; ml-report, «Отдел спрашивающего в
# поиске по справочнику»). Фаза 2, T7: в 9 из 18 провалов решение писало
# общую роль («руководитель», «непосредственный руководитель»), хотя отдел
# спрашивающего назван в вопросе («Я в ОКП», «выхожу в бухгалтерию»), а
# поиск по «руководитель» находил чужих руководителей (ЦУП и ОКП). Подвид:
# роль с отделом («руководитель административно-хозяйственной службы»), а
# глава в справочнике называется иначе (офис-менеджер, главный бухгалтер).

GENERIC_ROLES: tuple[str, ...] = (
    "руководитель",
    "непосредственный руководитель",
    "прямой руководитель",
    "линейный руководитель",
    "руководитель подразделения",
    "руководитель структурного подразделения",
    "руководитель подразделения-инициатора",
    "руководитель отдела",
    "руководитель службы",
    "начальник",
    "непосредственный начальник",
    "начальник отдела",
    "начальник подразделения",
    "начальник службы",
)
"""Общие роли: «чей» руководитель — не сказано. Сравнение — по набору основ
без притяжательных («мой руководитель», «руководителя моего подразделения»,
«непосредственного руководителя»). «Подразделения-инициатора» по основе
совпадает с «подразделения» (первые 5 букв)."""

HEAD_TITLES: tuple[str, ...] = (
    "руководитель",
    "начальник",
    "директор",
    "заведующий",
    "управляющий",
    "главный бухгалтер",
    "главный инженер",
    "офис-менеджер",
)
"""Должности главы подразделения — по порядку старшинства: должность
записи начинается с одной из них. «Главный» — только с профессией
(«главный специалист» — не глава). Офис-менеджер — глава АХС стенда."""

_POSSESSIVE = frozenset(
    [
        "мой",
        "моя",
        "мое",
        "моего",
        "моей",
        "моему",
        "моем",
        "мою",
        "мои",
        "моих",
        "наш",
        "наша",
        "наше",
        "нашего",
        "нашей",
        "нашему",
        "нашем",
        "нашу",
        "наши",
        "наших",
        "свой",
        "своя",
        "свое",
        "своего",
        "своей",
        "своему",
        "своем",
        "свою",
    ]
)
# Слова о себе: после них (через «мост») — подразделение спрашивающего.
_ASKER = (
    frozenset(
        [
            "я",
            "мы",
            "работаю",
            "работаем",
            "тружусь",
            "трудимся",
            "числюсь",
            "числимся",
            "выхожу",
            "выходим",
            "выйду",
            "устроился",
            "устроилась",
            "устраиваюсь",
            "перевели",
            "перевелся",
            "перевелась",
            "перевожусь",
        ]
    )
    | _POSSESSIVE
)
# Что может стоять между словом о себе и подразделением: предлоги, «на
# работу», наречия, должность спрашивающего («Я специалист ЦУП», «Я
# сотрудник АХС», «работаю менеджером в ОКП»). Остальное рвёт связь:
# «Я написал в бухгалтерию» — не про отдел спрашивающего.
_BRIDGE_WORDS = (
    frozenset(
        [
            "в",
            "во",
            "из",
            "на",
            "по",
            "у",
            "работу",
            "работе",
            "сейчас",
            "теперь",
            "уже",
            "тоже",
            "недавно",
            "только",
            "как",
            "отдел",
            "отделе",
            "отдела",
            "служба",
            "службе",
            "службы",
            "подразделение",
            "подразделении",
            "подразделения",
        ]
    )
    | _ASKER
)
_BRIDGE_STEMS = frozenset(
    [
        "специ",
        "сотру",
        "работ",
        "менед",
        "бухга",
        "эконо",
        "инжен",
        "юрист",
        "юриск",
        "анали",
        "логис",
        "кладо",
        "водит",
        "опера",
        "секре",
        "ассис",
        "помощ",
        "прогр",
        "разра",
        "стаже",
        "нович",
        "касси",
        "курье",
        "офис-",
        "старш",
        "ведущ",
        "главн",
        "младш",
        "новый",
        "новая",
        "новен",
    ]
)
_ASKER_WINDOW = 5
"""Слово о себе — не дальше 5 слов до подразделения."""
_CLAUSE = re.compile(r"[.!?;]+")


def _words(text: str) -> list[str]:
    return re.findall(r"[\w-]+", _normalize(text))


def _content(word: str) -> bool:
    return word not in _STOP and len(word) > 2 and not word.isdigit()


def _role_stems(role: str) -> frozenset[str]:
    return frozenset(
        _stem(word)
        for word in _words(role)
        if _content(word) and word not in _POSSESSIVE
    )


_GENERIC = frozenset(_role_stems(role) for role in GENERIC_ROLES)


def is_generic_role(role: str) -> bool:
    """Общая ли роль: «руководитель», «непосредственный руководитель»,
    «руководитель подразделения», «начальник», «мой руководитель» — во
    всех падежах (по основам). «Руководитель ОКП», «главный бухгалтер»,
    «руководитель казначейства» — не общие."""
    return _role_stems(role) in _GENERIC


def _units(entries: Sequence[Entry]) -> list[str]:
    """Подразделения справочника — поле `Entry.unit`, без повторов. Строки
    без имени («— Отдел персонала — подчиняется …») разбор пишет ролью без
    подразделения — их здесь нет."""
    out: list[str] = []
    for entry in entries:
        if entry.unit and entry.unit not in out:
            out.append(entry.unit)
    return out


def _unit_pattern(unit: str) -> tuple[list[str], list[str]]:
    """Основы названия по порядку и аббревиатуры (из скобок или по первым
    буквам)."""
    words = [w for w in _words(_PAREN.sub(" ", unit)) if _content(w)]
    abbrs = [a for part in _PAREN.findall(unit) for a in _words(part)]
    # Аббревиатуры нет в скобках — по первым буквам слов названия, если их
    # не меньше трёх: «Административно-хозяйственная служба» → «АХС».
    initials = "".join(p[0] for w in words for p in w.split("-") if p)
    if len(words) > 1 and len(initials) >= 3 and initials not in abbrs:
        abbrs.append(initials)
    return [_stem(w) for w in words], abbrs


def _mentions(words: list[str], entries: Sequence[Entry]) -> list[tuple[int, str]]:
    """(позиция первого слова, подразделение) для каждого упоминания.
    Название — все его содержательные слова подряд по основам
    («в административно-хозяйственной службе», «бухгалтер» → «Бухгалтерия»);
    аббревиатура — слово целиком или с окончанием до 2 букв («ЦУПе»)."""
    content = [i for i, w in enumerate(words) if _content(w)]
    stems = [_stem(words[i]) for i in content]
    out = []
    for unit in _units(entries):
        name, abbrs = _unit_pattern(unit)
        for i, word in enumerate(words):
            if any(
                word == a or (word.startswith(a) and len(word) - len(a) <= 2)
                for a in abbrs
                if len(a) >= 2
            ):
                out.append((i, unit))
        if not name:
            continue
        for j in range(len(stems) - len(name) + 1):
            if stems[j : j + len(name)] == name:
                out.append((content[j], unit))
    return sorted(out)


def unit_mentions(text: str, entries: Sequence[Entry]) -> list[str]:
    """Все подразделения справочника, названные в тексте, по порядку (без
    контекста: «Сколько сотрудников работает в ЦУП?» — тоже ЦУП)."""
    out: list[str] = []
    for clause in _CLAUSE.split(text):
        for _, unit in _mentions(_words(clause), entries):
            if unit not in out:
                out.append(unit)
    return out


def _about_asker(words: list[str], at: int) -> bool:
    """Перед подразделением — слово о себе через «мост» и без «не»
    («Работаю региональным менеджером (не в ОКП)» — не ОКП)."""
    if at and (
        words[at - 1] == "не"
        or (at > 1 and words[at - 2] == "не" and words[at - 1] in {"в", "во", "из"})
    ):
        return False
    k = at - 1
    while k >= 0 and at - k <= _ASKER_WINDOW:
        word = words[k]
        if word in _ASKER:
            return True
        if word not in _BRIDGE_WORDS and _stem(word) not in _BRIDGE_STEMS:
            return False
        k -= 1
    return False


def asker_unit(question: str, entries: Sequence[Entry]) -> str | None:
    """Подразделение спрашивающего из текста вопроса или None.

    Только подразделения справочника (`Entry.unit`) и только в контексте
    «о себе» в той же фразе: «Я в ОКП», «Я специалист ЦУП», «Работаю в
    бухгалтерии», «выхожу на работу в бухгалтерию», «Я бухгалтер» (основа
    «бухга» — «Бухгалтерия»), «Я из административно-хозяйственной службы».
    Не про спрашивающего: «Кто согласует закупку для ОКП?», «Сколько
    сотрудников работает в ЦУП?», «(не в ОКП)». Два разных подразделения
    «о себе» — None (не угадываем)."""
    found: set[str] = set()
    for clause in _CLAUSE.split(question):
        words = _words(clause)
        for at, unit in _mentions(words, entries):
            if _about_asker(words, at):
                found.add(unit)
    return found.pop() if len(found) == 1 else None


def _title_of(role: str, title: str) -> bool:
    """Должность начинается с должности главы (по основам; «ИТ-директор» —
    «директор» после дефиса)."""
    have = [_stem(w) for w in _words(role) if _content(w)]
    want = [_stem(w) for w in _words(title)]
    if have[: len(want)] == want:
        return True
    first = _words(role)[:1]
    return (
        len(want) == 1
        and bool(first)
        and "-" in first[0]
        and _stem(first[0].rsplit("-", 1)[1]) == want[0]
    )


def unit_head(entries: Sequence[Entry], unit: str) -> Entry | None:
    """Глава подразделения: запись этого подразделения, чья должность — из
    `HEAD_TITLES` (первая по старшинству, если такая ровно одна); если ни
    одной — единственная запись подразделения. Несколько записей одного
    уровня или ни одной подходящей — None.

    Где не сработает: глава называется иначе («координатор», «старший
    специалист») при нескольких записях; единственная запись — не глава
    (рядовой специалист); строки подразделения без имени (разбор не
    пишет их подразделением); две записи «руководитель» в одном отделе."""
    key = _normalize(unit)
    members = [e for e in entries if e.unit and _normalize(e.unit) == key]
    for title in HEAD_TITLES:
        heads = [e for e in members if _title_of(e.role, title)]
        if len(heads) == 1:
            return heads[0]
        if heads:
            return None
    return members[0] if len(members) == 1 else None


def role_unit(role: str, entries: Sequence[Entry]) -> str | None:
    """Подразделение, названное в самой роли, если остальное в роли — общая
    роль: «руководитель ОКП», «непосредственный руководитель
    административно-хозяйственной службы», «руководитель бухгалтерии».
    «Заместитель руководителя ЦУП», «главный бухгалтер» — None."""
    units = unit_mentions(role, entries)
    if len(units) != 1:
        return None
    name, abbrs = _unit_pattern(units[0])
    rest = _role_stems(role) - set(name) - {_stem(a) for a in abbrs}
    return units[0] if rest in _GENERIC else None


@dataclass(frozen=True)
class HeadMatch:
    """Глава подразделения вместо общей роли: какое подразделение, откуда
    оно («role» — названо в роли, «question» — отдел спрашивающего) и
    запись главы."""

    unit: str
    source: str
    head: Entry

    def note(self) -> str:
        """Для журнала: «Бухгалтерия (вопрос) → главный бухгалтер Имя»."""
        where = "вопрос" if self.source == "question" else "роль"
        return f"{self.unit} ({where}) → {self.head.role} {self.head.name}".strip()


def head_for_role(
    entries: Sequence[Entry], role: str, question: str
) -> HeadMatch | None:
    """Глава подразделения для общей роли или None (дальше — как было).

    1. Роль сама называет подразделение и в остальном общая («руководитель
       АХС») — глава этого подразделения, как бы ни называлась должность.
    2. Роль общая («руководитель», «непосредственный руководитель») и в
       вопросе есть отдел спрашивающего (`asker_unit`) — его глава.
    Роль не общая («директор по персоналу», «главный бухгалтер»,
    «руководитель казначейства») — None: подразделение спрашивающего в
    неё не подставляется. Подразделения нет в справочнике, отдел в
    вопросе не про спрашивающего, главу не определить — None."""
    unit, source = role_unit(role, entries), "role"
    if unit is None:
        if not is_generic_role(role):
            return None
        unit, source = asker_unit(question, entries), "question"
        if unit is None:
            return None
    head = unit_head(entries, unit)
    return HeadMatch(unit=unit, source=source, head=head) if head else None


def generic_role_miss(entries: Sequence[Entry], role: str, question: str) -> str:
    """Причина промаха справочника для общей роли без главы, иначе пусто.

    Решение Артёма 23.10: общая роль («руководитель») без отдела
    спрашивающего — промах, а не чужие руководители (`lookup` по
    «руководитель» находит всех с этой должностью — T7-19). Дальше —
    прежний маршрут: добор, ответ по тому, что есть, или отказ. Так же —
    если отдел есть, а главу не определить. Звать, когда `head_for_role`
    вернул None."""
    if not is_generic_role(role):
        return ""
    unit = asker_unit(question, entries)
    if unit is None:
        return "промах: общая роль без отдела спрашивающего"
    return f"промах: общая роль, главу «{unit}» не определить"
