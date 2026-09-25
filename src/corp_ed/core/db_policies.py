"""Правила, которые живут в самой базе, а не в коде приложения.

Модели описывают таблицы, но не триггеры и не политики RLS: этого нет
в Base.metadata, и create_all их не создаёт. SQL лежит здесь, в одном
месте, и его выполняют:
- миграции — на реальных базах;
- тестовая фикстура — после create_all, чтобы тесты проверяли то же
  поведение, что и в production.

Все операторы идемпотентны (CREATE OR REPLACE, DROP ... IF EXISTS):
изменение правила — новая миграция, которая вызывает ту же функцию.
"""

from sqlalchemy import text
from sqlalchemy.engine import Connection

AUDIT_RETENTION_DAYS = 365
"""Срок хранения журнала аудита. Год — ориентир для расследования
инцидентов. Константа, а не настройка: иначе тот, кто хочет замести
следы, укоротил бы срок переменной окружения."""


def audit_append_only_statements() -> list[str]:
    """Журнал аудита только дописывается.

    Триггер работает для любой роли, включая владельца таблицы: запись о
    действии не должна исчезать вместе с учёткой того, кто его совершил.
    Удалять можно только записи старше срока хранения.
    """
    return [
        f"""
        CREATE OR REPLACE FUNCTION audit_events_append_only() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                -- ON DELETE SET NULL у внешних ключей — это UPDATE: при
                -- удалении пользователя или компании ссылку обнулить можно,
                -- всё остальное менять нельзя.
                IF (NEW.id, NEW.action, NEW.target_type, NEW.target_id, NEW.ip,
                    NEW.request_id, NEW.details, NEW.created_at)
                   IS DISTINCT FROM
                   (OLD.id, OLD.action, OLD.target_type, OLD.target_id, OLD.ip,
                    OLD.request_id, OLD.details, OLD.created_at)
                   OR (NEW.tenant_id IS NOT NULL
                       AND NEW.tenant_id IS DISTINCT FROM OLD.tenant_id)
                   OR (NEW.actor_user_id IS NOT NULL
                       AND NEW.actor_user_id IS DISTINCT FROM OLD.actor_user_id)
                THEN
                    RAISE EXCEPTION 'audit_events is append-only';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.created_at > now() - interval '{AUDIT_RETENTION_DAYS} days' THEN
                RAISE EXCEPTION 'audit_events: records younger than retention';
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;
        """,
        "DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events",
        """
        CREATE TRIGGER audit_events_append_only
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION audit_events_append_only()
        """,
    ]


def all_statements() -> list[str]:
    return [*audit_append_only_statements()]


def apply_all(connection: Connection) -> None:
    """Выполнить все правила (тестовая фикстура после create_all)."""
    for statement in all_statements():
        connection.execute(text(statement))
