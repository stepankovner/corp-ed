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


TENANT_SETTING = "app.tenant_id"
"""Параметр сессии PostgreSQL, из которого политики RLS берут тенанта.

Ставится в начале каждой транзакции из current_tenant (core/database.py)
через set_config(..., is_local => true): значение живёт до конца
транзакции и не переезжает с соединением пула в чужой запрос."""

TENANT_TABLES = (
    "users",
    "materials",
    "chunks",
    "qa_log",
    "glossary_terms",
    "gap_clusters",
    "gap_cluster_questions",
)
"""Таблицы под RLS. Каждая тенант-модель обязана быть здесь — это
проверяет тест (tests/security/test_rls.py). Не входят: tenants (корень,
ищется при входе до того, как тенант известен), refresh_tokens (ищется
по хешу до входа), audit_events (пишется и без тенанта)."""

# NULLIF: пустая строка (тенант не выставлен) превращается в NULL, и
# сравнение даёт NULL — ни одной строки. Приведение ''::uuid упало бы с
# ошибкой, а нам нужно тихое «ничего не видно» (default deny).
_CURRENT_TENANT = f"NULLIF(current_setting('{TENANT_SETTING}', true), '')::uuid"


def rls_statements(tables: tuple[str, ...] = TENANT_TABLES) -> list[str]:
    """Row-Level Security: строки чужого тенанта не видны и не пишутся.

    Второй рубеж после ORM-хуков: они не покрывают сырой SQL и
    колоночные select, а политика в базе — покрывает всё, включая
    ошибку в коде, который про тенанта забыл (RISKS №1).

    FORCE — политика действует и для владельца таблицы. Обходят её
    только суперпользователь и роли с BYPASSRLS, поэтому приложение
    обязано работать под ролью без них (проверяется на старте).

    Следствие для МИГРАЦИЙ с данными: владелец схемы тоже видит ноль
    строк без app.tenant_id. DELETE/UPDATE по всем компаниям — через
    TRUNCATE или внутри ALTER TABLE ... NO FORCE / FORCE в той же
    транзакции (пример — ревизия 97d70ebf2e07).
    """
    statements: list[str] = []
    for table in tables:
        statements += [
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
            f"DROP POLICY IF EXISTS tenant_isolation ON {table}",
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = {_CURRENT_TENANT})
            WITH CHECK (tenant_id = {_CURRENT_TENANT})
            """,
        ]
    return statements


def all_statements() -> list[str]:
    return [*audit_append_only_statements(), *rls_statements()]


def apply_all(connection: Connection) -> None:
    """Выполнить все правила (тестовая фикстура после create_all)."""
    for statement in all_statements():
        connection.execute(text(statement))
