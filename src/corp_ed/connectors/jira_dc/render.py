"""Задача Jira → HTML страницы для общего конвейера (FetchedPage).

Описание и комментарии берутся из renderedFields: Jira сама переводит
вики-разметку в HTML, а ядро потом его чистит (connectors/html.py) —
свой разбор вики-разметки не нужен. Заголовок, проект, тип, статус и
авторы — простой текст из fields, экранируются здесь.

Комментарий с ограничением видимости (`visibility`: роль или группа)
в страницу не попадает: права страницы — права задачи, а такой
комментарий видят не все её читатели.
"""

from collections.abc import Iterable, Mapping
from html import escape
from typing import Any


def visible_comments(
    raw: Iterable[Mapping[str, Any]], rendered: Iterable[Mapping[str, Any]]
) -> list[tuple[Mapping[str, Any], str]]:
    """Пары (комментарий, HTML тела) без ограниченных по видимости.

    raw — fields.comment.comments (там visibility и дата в ISO),
    rendered — renderedFields.comment.comments (HTML тела; дата там
    «2 minutes ago»). Сопоставляются по id.
    """
    bodies = {
        str(item.get("id")): item.get("body")
        for item in rendered
        if isinstance(item, Mapping)
    }
    found: list[tuple[Mapping[str, Any], str]] = []
    for comment in raw:
        if not isinstance(comment, Mapping) or comment.get("visibility"):
            continue
        body = bodies.get(str(comment.get("id")))
        if not isinstance(body, str):
            body = comment.get("renderedBody")
        if isinstance(body, str) and body.strip():
            found.append((comment, body))
    return found


def issue_html(
    key: str,
    fields: Mapping[str, Any],
    description: str | None,
    comments: Iterable[tuple[Mapping[str, Any], str]],
) -> str:
    summary = str(fields.get("summary") or "")
    parts = [f"<h1>{escape(key)}: {escape(summary)}</h1>"]
    facts = [
        (label, _name(fields.get(field)))
        for label, field in (
            ("Проект", "project"),
            ("Тип", "issuetype"),
            ("Статус", "status"),
            ("Автор", "reporter"),
            ("Исполнитель", "assignee"),
        )
    ]
    shown = [f"{label}: {escape(value)}" for label, value in facts if value]
    if shown:
        parts.append(f"<p>{' · '.join(shown)}</p>")
    if description and description.strip():
        parts.append(f"<h2>Описание</h2>{description}")
    rendered = list(comments)
    if rendered:
        parts.append("<h2>Комментарии</h2>")
        for comment, body in rendered:
            author = _name(comment.get("author")) or "—"
            when = str(comment.get("created") or "")[:10]
            title = f"{author}, {when}" if when else author
            parts.append(f"<h3>{escape(title)}</h3>{body}")
    return "<html><body>" + "".join(parts) + "</body></html>"


def _name(value: Any) -> str:
    """Имя из объекта Jira: displayName пользователя или name проекта,
    типа, статуса."""
    if not isinstance(value, Mapping):
        return ""
    name = value.get("displayName") or value.get("name")
    return " ".join(str(name).split()) if name else ""
