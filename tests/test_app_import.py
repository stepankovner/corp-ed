import os
import subprocess
import sys


def test_importing_app_does_not_require_environment() -> None:
    """Импорт приложения не должен читать обязательные переменные.

    Конфиг читается там, где создаются объекты, а не на уровне модулей:
    иначе alembic и тесты нельзя запустить без полного .env, а сборка
    образа — без секретов. Проверяется в отдельном процессе с пустым
    окружением, потому что в текущем корп_ed.main уже импортирован.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import corp_ed.main"],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
