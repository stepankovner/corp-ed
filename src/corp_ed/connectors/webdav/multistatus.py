"""Разбор ответа PROPFIND (207 Multi-Status, RFC 4918 §13–14).

Тело — враждебный ввод: XML разбирается defusedxml без DTD (сущности и
внешние ссылки отвергаются), из каждого <d:response> берутся только
href и свойства из propstat со статусом 200. Неизвестные серверу
свойства (oc:fileid у NAS) приходят отдельным propstat с 404 — их нет.

href здесь — как прислал сервер; можно ли ему верить (тот же хост, тот
же корень, прямой потомок папки), решает обход (adapter.py).
"""

from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException  # type: ignore[import-untyped]
from defusedxml.ElementTree import fromstring  # type: ignore[import-untyped]

from corp_ed.connectors.base import AdapterError

DAV = "{DAV:}"
OC = "{http://owncloud.org/ns}"

PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:prop>'
    b"<d:resourcetype/><d:getetag/><d:getlastmodified/><d:getcontentlength/>"
    b"<d:displayname/><oc:fileid/>"
    b"</d:prop></d:propfind>"
)
"""Свойства листинга. oc:fileid — сквозной номер файла Nextcloud и
ownCloud: по нему общий файл двух сотрудников — один документ."""

PRINCIPAL_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/>'
    b"</d:prop></d:propfind>"
)
"""RFC 5397: кто мы на сервере — путь principals/users/<uid>/."""


@dataclass(frozen=True)
class DavEntry:
    href: str
    is_collection: bool
    etag: str = ""
    modified: datetime | None = None
    modified_raw: str = ""
    size: int | None = None
    display_name: str = ""
    file_id: str = ""
    principal: str = ""


def parse_multistatus(body: bytes) -> list[DavEntry]:
    """207 → элементы. Не XML или не multistatus — AdapterError("not_webdav"):
    вместо WebDAV ответила страница входа или не тот адрес."""
    try:
        root = fromstring(body, forbid_dtd=True)
    except (ParseError, DefusedXmlException, ValueError) as exc:
        raise AdapterError("not_webdav") from exc
    if root.tag != f"{DAV}multistatus":
        raise AdapterError("not_webdav")
    entries: list[DavEntry] = []
    for response in root.findall(f"{DAV}response"):
        href = (response.findtext(f"{DAV}href") or "").strip()
        if not href:
            continue
        props = _found_props(response)
        entries.append(_entry(href, props))
    return entries


def _found_props(response: Element) -> dict[str, Element]:
    found: dict[str, Element] = {}
    for propstat in response.findall(f"{DAV}propstat"):
        status = propstat.findtext(f"{DAV}status") or "HTTP/1.1 200 OK"
        if " 200 " not in f"{status.strip()} ":
            continue
        prop = propstat.find(f"{DAV}prop")
        if prop is None:
            continue
        for child in prop:
            found[child.tag] = child
    return found


def _entry(href: str, props: dict[str, Element]) -> DavEntry:
    resourcetype = props.get(f"{DAV}resourcetype")
    is_collection = (
        resourcetype is not None and resourcetype.find(f"{DAV}collection") is not None
    )
    modified_raw = _text(props, f"{DAV}getlastmodified")
    principal = props.get(f"{DAV}current-user-principal")
    return DavEntry(
        href=href,
        is_collection=is_collection,
        etag=_etag(_text(props, f"{DAV}getetag")),
        modified=_http_date(modified_raw),
        modified_raw=modified_raw,
        size=_size(_text(props, f"{DAV}getcontentlength")),
        display_name=_text(props, f"{DAV}displayname"),
        file_id=_text(props, f"{OC}fileid"),
        principal=(
            (principal.findtext(f"{DAV}href") or "").strip()
            if principal is not None
            else ""
        ),
    )


def _text(props: dict[str, Element], tag: str) -> str:
    element = props.get(tag)
    return (element.text or "").strip() if element is not None else ""


def _etag(value: str) -> str:
    """Кавычки и W/ — синтаксис заголовка ETag, не значение: W/"abc" → abc."""
    return value.removeprefix("W/").strip('"')


def _size(value: str) -> int | None:
    return int(value) if value.isdigit() else None


def _http_date(value: str) -> datetime | None:
    """getlastmodified — дата HTTP (RFC 1123), не ISO 8601."""
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
