from pydantic import BaseModel, ConfigDict


class RequestModel(BaseModel):
    """База для тел запросов: неизвестные поля — ошибка 422.

    По умолчанию pydantic их молча отбрасывает. Для API это плохо: поле
    tenant_id или role, присланное «на пробу», не должно даже доходить
    до разбора — mass assignment (OWASP API3:2023) закрывается на входе,
    а клиент сразу узнаёт об опечатке в имени поля.
    """

    model_config = ConfigDict(extra="forbid")
