"""Адаптер MediaWiki (вики «у себя»): страницы и файлы через Action API
`api.php` по бот-паролю служебной учётки (режим organization).

Прав на отдельные страницы в MediaWiki нет, почт пользователей API не
отдаёт: документ видят сотрудники из сопоставления «логин в вики =
почта», у кого в вики есть право чтения (rights.py). При расширениях,
закрывающих пространства имён (Lockdown и подобные), синхронизируются
только пространства, перечисленные админом; «вся компания» не ставится.

Проверен живьём 15.10.2026 на MediaWiki 1.43.11 в Docker
(`tests/live/mediawiki`); в каталог — после проверки у клиента
(`cli connector-check --kind mediawiki`), пока вид скрыт (preview).
"""

from corp_ed.connectors.mediawiki.adapter import KIND, SPEC, register

__all__ = ["KIND", "SPEC", "register"]
