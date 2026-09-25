# Фикстуры HTTP-клиента живут в tests/api/conftest.py. Тесты периметра
# используют те же — импорт делает их видимыми в этом пакете.
from tests.api.conftest import account, admin_account, api  # noqa: F401
