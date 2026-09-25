"""Storage-формат Confluence (XHTML с макросами ac:/ri:) → обычный HTML.

Ядро чистит HTML общей очисткой (connectors/html.py), но макросы
Confluence — не HTML: без подготовки текст кода, панелей и задач либо
пропал бы, либо превратился бы в кашу. Здесь — только структура:
код → <pre>, панели и разметка колонок → содержимое, ссылки на
страницы и вложения → текст, служебные макросы (оглавление, Jira,
включения) — вон. Всё неизвестное остаётся как есть: текст внутри
доживёт до Markdown.
"""

from html import escape

from bs4 import BeautifulSoup, Tag

# Макросы, содержимое которых — текст документа: оставить тело.
_UNWRAP_MACROS = frozenset(
    {
        "info",
        "note",
        "warning",
        "tip",
        "panel",
        "expand",
        "excerpt",
        "section",
        "column",
        "details",
        "status",
        "anchor",
    }
)
# Код и преформатированный текст: тело — CDATA.
_CODE_MACROS = frozenset({"code", "noformat"})
# Динамика и навигация: в тексте документа их нет.
_DROP_MACROS = frozenset(
    {
        "toc",
        "jira",
        "jiraissues",
        "include",
        "children",
        "recently-updated",
        "contentbylabel",
        "pagetree",
        "livesearch",
        "gallery",
        "attachments",
        "widget",
        "profile",
        "create-from-template",
        "excerpt-include",
        "multiexcerpt-include",
        "view-file",
        "drawio",
        "gliffy",
    }
)


def storage_to_html(title: str, storage: str) -> str:
    soup = BeautifulSoup(storage, "html.parser")
    for macro in soup.find_all("ac:structured-macro"):
        if not isinstance(macro, Tag):
            continue
        name = str(macro.get("ac:name") or "").lower()
        if name in _CODE_MACROS:
            body = macro.find("ac:plain-text-body")
            pre = soup.new_tag("pre")
            pre.string = body.get_text() if body is not None else macro.get_text()
            macro.replace_with(pre)
        elif name in _DROP_MACROS:
            macro.decompose()
        else:
            # Панели, колонки и незнакомые макросы: тело наружу, параметры — вон.
            for parameter in macro.find_all("ac:parameter"):
                parameter.decompose()
            macro.unwrap()
    for body in soup.find_all(["ac:rich-text-body", "ac:plain-text-body"]):
        if isinstance(body, Tag):
            body.unwrap()
    for link in soup.find_all("ac:link"):
        if isinstance(link, Tag):
            link.replace_with(_link_text(link))
    for image in soup.find_all("ac:image"):
        if isinstance(image, Tag):
            image.decompose()
    for task_list in soup.find_all("ac:task-list"):
        if isinstance(task_list, Tag):
            task_list.name = "ul"
    for task in soup.find_all("ac:task"):
        if isinstance(task, Tag):
            for extra in task.find_all(
                ["ac:task-id", "ac:task-status", "ac:task-uuid"]
            ):
                extra.decompose()
            task.name = "li"
    for body in soup.find_all("ac:task-body"):
        if isinstance(body, Tag):
            body.unwrap()
    for layout in soup.find_all(
        ["ac:layout", "ac:layout-section", "ac:layout-cell", "ac:placeholder"]
    ):
        if isinstance(layout, Tag):
            if layout.name == "ac:placeholder":
                layout.decompose()
            else:
                layout.unwrap()
    return f"<h1>{escape(title)}</h1>\n{soup}"


def _link_text(link: Tag) -> str:
    body = link.find(["ac:link-body", "ac:plain-text-link-body"])
    if body is not None and body.get_text().strip():
        return body.get_text().strip()
    page = link.find("ri:page")
    if isinstance(page, Tag) and page.get("ri:content-title"):
        return str(page.get("ri:content-title"))
    attachment = link.find("ri:attachment")
    if isinstance(attachment, Tag) and attachment.get("ri:filename"):
        return str(attachment.get("ri:filename"))
    user = link.find("ri:user")
    if isinstance(user, Tag):
        return "@" + str(user.get("ri:username") or user.get("ri:userkey") or "user")
    return link.get_text().strip()
