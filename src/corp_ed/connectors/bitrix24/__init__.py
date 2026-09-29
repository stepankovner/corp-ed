"""Адаптер Битрикс24: диск (disk.*) и база знаний (landing.*) через
OAuth-приложение от имени каждого сотрудника (режим per_user).

Состав: client — REST-вызовы, темп, коды ошибок, продление токена;
oauth — обмен кода и продление; disk и knowledge_base — обход модулей;
adapter — спецификация вида и сборка. Регистрируется в
default_registry().
"""

from corp_ed.connectors.bitrix24.adapter import KIND, SPEC, register

__all__ = ["KIND", "SPEC", "register"]
