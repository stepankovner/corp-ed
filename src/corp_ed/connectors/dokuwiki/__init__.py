"""Адаптер DokuWiki (своя установка): страницы и медиафайлы с правами,
которые считает сама DokuWiki (`core.aclCheck`) для сотрудников из
списка «логин почта», заданного администратором.

Только JSON-RPC (`/lib/exe/jsonrpc.php`) нового API — DokuWiki 2024-02-06
«Kaos» и новее; XML-RPC старых версий не поддерживается. Пользователей
с почтами API DokuWiki не отдаёт — без списка права не перенести.
Ограничения прав — в докстроке adapter.py. Проверено живьём на
2026-07-14c «Mort» в Docker (tests/live/dokuwiki/README.md); вид скрыт
(`preview`), пока его не включат.
"""

from corp_ed.connectors.dokuwiki.adapter import SPEC, register

__all__ = ["SPEC", "register"]
