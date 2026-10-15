"""Спецификация вида amocrm и сборка адаптера.

Режим per_user с OAuth: админ создаёт в аккаунте интеграцию (Настройки
→ Интеграции → Создать; доступы «Доступ к данным аккаунта» и «Доступ к
файлам», Redirect URI — наша ручка обратного вызова), вводит адрес
аккаунта и ID интеграции в config, секретный ключ — в учётные данные
подключения; каждый сотрудник авторизует интеграцию своим входом, и
обход идёт его токеном — «Токен имеет права пользователя, который
предоставил доступ» (oauth/step-by-step). В JWT токена — id
пользователя и аккаунта.

Ограничение amoCRM: окно авторизации предлагает пользователю аккаунты,
где он администратор (oauth/step-by-step). Сотрудник без прав
администратора в amoCRM, по документации, подключиться не сможет —
грант просто не создаётся; общий токен администратора для остальных
не используется (он видел бы больше, чем они). Проверить на живом
аккаунте.

check сверяет поддомен аккаунта токена с настроенным: токен другого
аккаунта (администратор нескольких выбрал не тот) не принимается.
Долгосрочный токен (credentials["access_token"] без refresh) принимает
только cli connector-check: в форме его нет.
"""

from collections.abc import AsyncIterator, Mapping, Sequence

from corp_ed.connectors.amocrm import files as files_module
from corp_ed.connectors.amocrm.client import MIN_INTERVAL, AmoAuth, AmoClient
from corp_ed.connectors.amocrm.oauth import AmoOAuth, account_root
from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    AdapterOptions,
    FetchedContent,
    RemoteDocument,
)
from corp_ed.connectors.common import (
    OAUTH_CALLBACK_PATH,
    Recorder,
    TokenSet,
    to_int,
)
from corp_ed.connectors.registry import (
    AdapterRegistry,
    FieldSpec,
    KindSpec,
    ModuleSpec,
    UserAuth,
)
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import ConnectorMode

KIND = "amocrm"


def _pipelines(config: Mapping[str, str]) -> list[str]:
    return [p.strip() for p in config.get("pipelines", "").split(",") if p.strip()]


def _check_config(config: Mapping[str, str]) -> str | None:
    if account_root(config.get("account", "")) is None:
        return "account_invalid"
    if any(not p.isdigit() for p in _pipelines(config)):
        return "pipelines_invalid"
    return None


SPEC = KindSpec(
    kind=KIND,
    title="amoCRM / Kommo (файлы сделок, контактов и компаний)",
    mode=ConnectorMode.PER_USER,
    user_auth=UserAuth.OAUTH,
    modules=(
        ModuleSpec(files_module.MODULE_LEAD_FILES, "Файлы сделок"),
        ModuleSpec(files_module.MODULE_CONTACT_FILES, "Файлы контактов"),
        ModuleSpec(files_module.MODULE_COMPANY_FILES, "Файлы компаний"),
    ),
    config_fields=(
        FieldSpec(
            "account",
            "Адрес аккаунта: https://компания.amocrm.ru (или https://компания.kommo.com)",
        ),
        FieldSpec("client_id", "ID интеграции (client_id)"),
        FieldSpec(
            "pipelines",
            "ID воронок сделок через запятую (пусто — все)",
            required=False,
        ),
    ),
    app_credential_fields=(
        FieldSpec("client_secret", "Секретный ключ интеграции", secret=True),
    ),
    credential_fields=(),
    url_field="account",
    config_check=_check_config,
    extra={
        "app_scopes": "crm,files",
        "app_scopes_title": "Доступ к данным аккаунта, Доступ к файлам",
        "oauth_callback_path": OAUTH_CALLBACK_PATH,
        "user_requirement": "администратор аккаунта amoCRM",
    },
    preview=True,
)


class AmoCrmAdapter:
    def __init__(
        self,
        client: AmoClient,
        *,
        max_bytes: int,
        pipelines: Sequence[int] = (),
    ) -> None:
        self._client = client
        self._max_bytes = max_bytes
        self._pipelines = tuple(pipelines)
        self._user_id: str | None = None
        self._checked = False

    @property
    def refreshed_credentials(self) -> Mapping[str, str] | None:
        return self._client.refreshed_credentials

    @property
    def external_user_id(self) -> str | None:
        return self._user_id

    async def check(self) -> None:
        """Кто мы и в каком аккаунте; адрес сервиса файлов."""
        try:
            account = await self._client.api("account", {"with": "drive_url"})
        except AdapterError as exc:
            if exc.code in {"forbidden", "not_found"}:
                # Аккаунт заблокирован за превышение лимитов или интеграция
                # отключена — не вопрос одного сотрудника.
                raise AdapterConfigError(f"account_{exc.code}") from exc
            raise
        if account is None:
            raise AdapterError("account_empty")
        subdomain = str(account.get("subdomain") or "").lower()
        if subdomain != self._client.subdomain:
            raise AdapterAuthError("account_mismatch")
        user_id = account.get("current_user_id")
        if user_id is None:
            raise AdapterAuthError("user_unknown")
        drive = account.get("drive_url")
        if not isinstance(drive, str) or not drive:
            raise AdapterConfigError("drive_url_missing")
        self._client.set_drive(drive)
        self._user_id = str(user_id)
        self._checked = True

    async def list(self, modules: Sequence[str]) -> AsyncIterator[RemoteDocument]:
        if not self._checked:
            await self.check()
        module = files_module.EntityFilesModule(
            self._client, max_bytes=self._max_bytes, pipelines=self._pipelines
        )
        async for document in module.walk(set(modules)):
            yield document

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedContent:
        if not document.external_id.startswith(files_module.PREFIX):
            raise AdapterError("unknown_document")
        if not self._checked:
            await self.check()
        module = files_module.EntityFilesModule(self._client, max_bytes=max_bytes)
        return await module.fetch(document, max_bytes=max_bytes)


def build_oauth(
    http: OutboundClient,
    config: Mapping[str, str],
    client_secret: str,
    settings: ConnectorSettings,
) -> AmoOAuth:
    client_id = config.get("client_id", "")
    if not client_id or not client_secret:
        raise AdapterConfigError("app_credentials_missing")
    if not settings.oauth_callback_url:
        # redirect_uri обязателен при обмене и продлении и должен совпасть
        # с настройками интеграции.
        raise AdapterConfigError("oauth_callback_url_missing")
    return AmoOAuth(
        http,
        account=config.get("account", ""),
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=settings.oauth_callback_url,
    )


def build_adapter(
    config: Mapping[str, str],
    credentials: Mapping[str, str],
    http: OutboundClient,
    settings: ConnectorSettings,
    *,
    recorder: Recorder | None = None,
    min_interval: float | None = None,
) -> AmoCrmAdapter:
    if problem := _check_config(config):
        raise AdapterConfigError(problem)
    access = credentials.get("access_token", "")
    if not access:
        raise AdapterAuthError("credentials_missing")
    refresh = credentials.get("refresh_token", "")
    oauth = (
        build_oauth(http, config, credentials.get("client_secret", ""), settings)
        if refresh
        else None
    )
    auth = AmoAuth(
        TokenSet(
            access_token=access,
            refresh_token=refresh,
            expires_at=to_int(credentials.get("expires_at")) or 0,
        ),
        oauth,
    )
    client = AmoClient(
        http,
        account=config["account"],
        auth=auth,
        recorder=recorder,
        min_interval=MIN_INTERVAL if min_interval is None else min_interval,
    )
    return AmoCrmAdapter(
        client,
        max_bytes=settings.download_limit_bytes,
        pipelines=[int(p) for p in _pipelines(config)],
    )


def register(registry: AdapterRegistry, settings: ConnectorSettings) -> None:
    def factory(
        spec: KindSpec,
        config: Mapping[str, str],
        credentials: Mapping[str, str],
        http: OutboundClient,
        options: AdapterOptions,
    ) -> AmoCrmAdapter:
        return build_adapter(
            config,
            credentials,
            http,
            settings,
            recorder=options.recorder,
            min_interval=0.0 if options.fast else None,
        )

    def oauth(
        spec: KindSpec,
        config: Mapping[str, str],
        app_credentials: Mapping[str, str],
        http: OutboundClient,
    ) -> AmoOAuth:
        return build_oauth(
            http, config, app_credentials.get("client_secret", ""), settings
        )

    registry.register(SPEC, factory, oauth)
