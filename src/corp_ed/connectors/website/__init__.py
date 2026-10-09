"""Публичный сайт или справочный центр (режим organization без ключей):
robots.txt, карты сайта, обход ссылок раздела, файлы по ссылкам.
Документы видит вся компания — содержимое публичное.

Сверено с RFC 9309 (robots.txt) и протоколом sitemaps.org 0.9; на живых
сайтах не проверено — вид скрыт до проверки (KindSpec.preview).
"""

from corp_ed.connectors.website.adapter import KIND, SPEC, register

__all__ = ["KIND", "SPEC", "register"]
