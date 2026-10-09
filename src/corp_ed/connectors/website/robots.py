"""robots.txt публичного сайта (RFC 9309).

Стандартный urllib.robotparser не знает шаблонов * и $ и берёт первое
совпавшее правило, а не самое длинное — для сайтов, которые пишут правила
под Яндекс и Google, это неверные ответы. Здесь — то, что требует RFC:

- группа нашего агента (имя без учёта регистра), иначе группа *; подряд
  идущие строки User-agent — одна группа; групп одного агента несколько —
  правила складываются;
- побеждает самое длинное совпавшее правило, при равной длине — Allow;
  `*` — любая последовательность, `$` в конце — конец пути; пустой
  Disallow ничего не запрещает;
- пути сравниваются после нормализации процентного кодирования
  («/а» и «/%D0%B0» — один путь);
- Crawl-delay и Sitemap — не часть RFC, но их понимают Яндекс и Bing:
  пауза между запросами и адреса карт сайта.

Мусорные строки пропускаются. Разбирается строка — сетевых загрузок нет.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote

MAX_ROBOTS_BYTES = 500 * 1024
"""RFC 9309: читать не меньше 500 КиБ; дальше — не читаем."""
_SAFE = "!$&'()*+,;=:@/?~-._"


@dataclass(frozen=True)
class _Rule:
    allow: bool
    pattern: re.Pattern[str]
    length: int


@dataclass(frozen=True)
class RobotsRules:
    rules: tuple[_Rule, ...] = ()
    crawl_delay: float | None = None
    sitemaps: tuple[str, ...] = field(default_factory=tuple)

    def allowed(self, path: str) -> bool:
        """Можно ли запрашивать путь (с query) по правилам группы."""
        target = _normalize(path or "/")
        best: _Rule | None = None
        for rule in self.rules:
            if not rule.pattern.match(target):
                continue
            if (
                best is None
                or rule.length > best.length
                or (rule.length == best.length and rule.allow and not best.allow)
            ):
                best = rule
        return best is None or best.allow


ALLOW_ALL = RobotsRules()
"""robots.txt нет (4xx): по RFC 9309 обходить можно всё."""


def parse_robots(text: str, agent: str) -> RobotsRules:
    agent = agent.lower()
    groups: list[tuple[set[str], list[tuple[str, str]]]] = []
    sitemaps: list[str] = []
    current: tuple[set[str], list[tuple[str, str]]] | None = None
    collecting_agents = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        name, sep, value = line.partition(":")
        if not sep:
            continue
        name = name.strip().lower()
        value = value.strip()
        if name == "sitemap":
            if value and value not in sitemaps:
                sitemaps.append(value)
            continue
        if name == "user-agent":
            if current is None or not collecting_agents:
                current = (set(), [])
                groups.append(current)
            current[0].add(value.lower())
            collecting_agents = True
            continue
        if current is None:
            continue
        collecting_agents = False
        if name in ("allow", "disallow", "crawl-delay"):
            current[1].append((name, value))

    own = [lines for agents, lines in groups if agent in agents]
    chosen = own or [lines for agents, lines in groups if "*" in agents]
    rules: list[_Rule] = []
    delay: float | None = None
    for lines in chosen:
        for name, value in lines:
            if name == "crawl-delay":
                try:
                    parsed = float(value)
                except ValueError:
                    continue
                if parsed >= 0:
                    delay = parsed
                continue
            if not value:
                # Пустой Disallow — «можно всё»; пустой Allow — ничего не меняет.
                continue
            rules.append(_rule(name == "allow", value))
    return RobotsRules(tuple(rules), delay, tuple(sitemaps))


def _normalize(path: str) -> str:
    return quote(unquote(path), safe=_SAFE + "%")


def _rule(allow: bool, value: str) -> _Rule:
    pattern = _normalize(value)
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return _Rule(
        allow=allow,
        pattern=re.compile(regex + (r"\Z" if anchored else ""), re.DOTALL),
        length=len(pattern),
    )
