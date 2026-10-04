"""Вымышленная компания для песочницы на сайте (ТЗ §1).

Документы — в documents/*.md: первая строка «# Название», дальше текст в
Markdown, как его нарезает индексация. Факты те же, что в заготовленном
демо на главной (frontend/src/site/demoScenarios.ts): посетитель, задав
тот же вопрос в песочнице, получит тот же ответ уже от kronto.

Поменять документ — поправить файл: `cli demo setup` при выкатке обновит
текст и переиндексирует только изменённые.
"""

from dataclasses import dataclass
from importlib import resources

COMPANY_NAME = "ООО «Меридиан Строй»"
"""Как компания называется на странице песочницы."""

TENANT_NAME = "Меридиан Строй — демо сайта"
"""Как она видна команде в нашей панели: сразу понятно, что это не клиент."""

SUGGESTED_QUESTIONS = (
    "Как оформить командировку на объект?",
    "До какой суммы можно купить материалы без тендера?",
    "Где шаблон акта скрытых работ?",
    "Сколько дней удалёнки положено проектировщикам?",
)
"""Готовые вопросы на странице; на последний в документах ответа нет —
песочница показывает честный отказ."""


@dataclass(frozen=True)
class DemoDocument:
    title: str
    content: str


def documents() -> list[DemoDocument]:
    """Документы по порядку имён файлов."""
    folder = resources.files(__package__).joinpath("documents")
    result = []
    for entry in sorted(folder.iterdir(), key=lambda item: item.name):
        if not entry.name.endswith(".md"):
            continue
        heading, _, body = entry.read_text(encoding="utf-8").partition("\n")
        if not heading.startswith("# "):
            raise ValueError(f"{entry.name}: первая строка — «# Название»")
        result.append(DemoDocument(title=heading[2:].strip(), content=body.strip()))
    return result
