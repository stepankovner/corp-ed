import structlog
from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = structlog.get_logger()


async def domain_fallback_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc)},
    )


async def conflict_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(exc)},
    )


async def not_found_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": str(exc)},
    )


async def permission_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={"detail": str(exc)},
    )


async def invalid_credentials_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=401, content={"detail": "Неверный логин или пароль"}
    )


async def not_authenticated_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": str(exc)},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def rate_limited_handler(request: Request, exc: Exception) -> JSONResponse:
    retry_after = getattr(exc, "retry_after", 60)
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"detail": str(exc)},
        headers={"Retry-After": str(retry_after)},
    )


async def service_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": str(exc)},
        headers={"Retry-After": "30"},
    )


async def unacceptable_file_handler(request: Request, exc: Exception) -> JSONResponse:
    code = getattr(exc, "code", "unsupported_format")
    return JSONResponse(
        status_code=(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
            if code == "unsupported_format"
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        ),
        content={"detail": str(exc), "code": code},
    )


async def duplicate_material_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "detail": str(exc),
            "material_id": str(getattr(exc, "material_id", "")),
        },
    )


async def credits_exhausted_handler(request: Request, exc: Exception) -> JSONResponse:
    """402: пул компании на месяц исчерпан.

    code — чтобы фронт показал отдельный экран, а не общую ошибку:
    повтор запроса здесь не поможет до следующего месяца.
    """
    return JSONResponse(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        content={"detail": str(exc), "code": "credits_exhausted"},
    )


async def connector_limit_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(exc), "code": getattr(exc, "code", "connector_limit")},
    )


async def invalid_connector_config_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": str(exc), "code": getattr(exc, "code", "invalid_config")},
    )


async def weak_password_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": str(exc)},
    )


async def llm_error_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    logger.warning(
        "llm_unavailable",
        error=str(exc),
        path=request.url.path,
    )

    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"detail": "Сервис языковой модели недоступен, попробуйте позже"},
    )


async def validation_error_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """422 без эха входных данных.

    Стандартный ответ FastAPI кладёт в каждую ошибку поле input — то, что
    прислал клиент. Для /auth/login это пароль, для /users — пароль
    нового сотрудника, и ответ с ним оседает в логах прокси и в
    инструментах разработчика. Оставляем только где ошибка и какая.
    """
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "detail": [
                {
                    "loc": list(error.get("loc", ())),
                    "msg": error.get("msg", ""),
                    "type": error.get("type", ""),
                }
                for error in errors
            ]
        },
    )


async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Ошибки-признаки бага (нет тенанта в контексте, чужой tenant_id).

    Клиент исправить их не может, подробности ему не нужны и опасны:
    текст про tenant_id подсказывает, где искать дыру в изоляции.
    """
    logger.error(
        "internal_error",
        error_type=type(exc).__name__,
        path=request.url.path,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Внутренняя ошибка сервера"},
    )
