"""Человеческое название устройства по User-Agent: «Chrome, Windows».

Для списка сеансов и писем о входе (ТЗ §3). Только подсказка: заголовок
задаёт клиент, решений на нём не принимаем.
"""

_BROWSERS = (
    ("YaBrowser", "Яндекс Браузер"),
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
)
_SYSTEMS = (
    ("iPhone", "iPhone"),
    ("iPad", "iPad"),
    ("Android", "Android"),
    ("Windows", "Windows"),
    ("Mac OS X", "macOS"),
    ("Macintosh", "macOS"),
    ("Linux", "Linux"),
)


def describe(user_agent: str | None) -> str | None:
    if not user_agent:
        return None
    browser = next((name for mark, name in _BROWSERS if mark in user_agent), None)
    system = next((name for mark, name in _SYSTEMS if mark in user_agent), None)
    parts = [p for p in (browser, system) if p]
    return ", ".join(parts) or None
