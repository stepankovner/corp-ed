"""Схема OpenAPI для фронта: python -m corp_ed.openapi > frontend/openapi.json

Фронт генерирует из неё типы API (`npm run gen:api`), а CI проверяет,
что закоммиченная схема совпадает с кодом: ручку поменяли, а фронт не
пересобрали — сборка падает, а не ломается у пользователя. В production
/openapi.json выключен, поэтому схема берётся из приложения напрямую.
"""

import json
import os
import sys


def main() -> int:
    # Схеме не нужны ни база, ни ключи: только для импорта приложения.
    os.environ.setdefault("SECRET_KEY", "openapi-export-" + "x" * 32)
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/openapi")
    os.environ.setdefault("LLM_PROVIDER", "fake")
    from corp_ed.main import app

    schema = app.openapi()
    sys.stdout.write(json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
