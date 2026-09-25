"""Адаптер Яндекс 360: Диск от имени каждого сотрудника (режим per_user,
OAuth Яндекс ID). Вики — следующим шагом, когда будет документация API.

Написан **без доступа к документации Яндекса** (сетевая политика
среды), по устоявшемуся REST Диска (`cloud-api.yandex.net/v1/disk`) и
OAuth Яндекс ID (`oauth.yandex.ru`): формы ответов — в поддельном
сервере тестов, сверить с живым Диском (RISKS №38).
"""

from corp_ed.connectors.yandex.adapter import KIND, SPEC, register

__all__ = ["KIND", "SPEC", "register"]
