"""Адаптер Яндекс 360 от имени каждого сотрудника (режим per_user, OAuth
Яндекс ID): личный Диск, общие диски организации, Вики.

Сверен с документацией Яндекс ID, REST API Диска и публичного API Вики
28.09; живым Диском и Вики не проверен (RISKS №38, №43).
"""

from corp_ed.connectors.yandex.adapter import KIND, SPEC, register

__all__ = ["KIND", "SPEC", "register"]
