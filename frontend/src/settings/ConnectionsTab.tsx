import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderClosed, FolderLock, Link2, Plug } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";

import { api, unwrap, type Schemas } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { isAdmin, useMe } from "../auth/context";
import { filled } from "../admin/connectorModel";
import { SecretInputs } from "../admin/connectorFields";
import { describeCode } from "../lib/codes";
import { formatNumber, plural } from "../lib/format";
import { useDocumentTitle } from "../lib/title";
import { safeHttpUrl } from "../lib/url";
import styles from "../admin/Admin.module.css";
import { ConfirmDialog } from "../admin/common";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Notice } from "../ui/Notice";
import { EmptyState } from "../ui/Page";
import pageStyles from "../ui/Page.module.css";
import { PageSpinner } from "../ui/Spinner";
import { Section } from "./common";
import settingsStyles from "./Settings.module.css";

type Mine = Schemas["MyConnectorResponse"];
type Sources = Schemas["MySourcesResponse"];

/**
 * «Мои подключения» в настройках (ТЗ §4, бывшие «Мои источники»):
 * источники, которые сотрудник подключает своим аккаунтом (режим
 * per_user), — kronto видит в них только то, что доступно ему самому.
 * Сверху — «Где ищет ассистент» (ТЗ §5): папки с документами, которые
 * сотрудник видит, и все источники компании.
 * Возврат с портала приходит на /sources?status=ok|error&connector_id=&
 * error_code= и переадресуется сюда с теми же параметрами.
 */
export function ConnectionsTab() {
  useDocumentTitle("Мои подключения");
  const me = useMe();
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [returned] = useState(() => ({
    status: params.get("status"),
    code: params.get("error_code"),
  }));
  const [disconnecting, setDisconnecting] = useState<Mine | null>(null);
  const mine = useQuery({
    queryKey: ["connectors", "mine"],
    queryFn: () => unwrap(api.GET("/api/v1/connectors/mine")),
  });

  // Параметры возврата показали — убираем из адреса, чтобы не повторять при обновлении.
  useEffect(() => {
    if (params.has("status")) setParams({}, { replace: true });
  }, [params, setParams]);

  const start = useMutation({
    mutationFn: async (id: string) => {
      const { authorize_url } = await unwrap(
        api.POST("/api/v1/connectors/{connector_id}/oauth/start", {
          params: { path: { connector_id: id } },
        }),
      );
      // Переходим только по http(s): javascript: и прочее не открываем.
      const url = safeHttpUrl(authorize_url);
      if (!url) {
        throw new ApiError(502, "Источник вернул некорректный адрес входа. Попробуйте позже.");
      }
      return url;
    },
    onSuccess: (url) => {
      window.location.assign(url);
    },
  });

  return (
    <>
      <div className={settingsStyles.stack}>
        {returned.status === "ok" ? (
          <Notice kind="ok" title="Аккаунт подключён">
            Документы появятся в ответах после ближайшей синхронизации — обычно в течение часа.
          </Notice>
        ) : null}
        {returned.status === "error" ? (
          <Notice kind="error" title="Не удалось подключить">
            {describeConnectCode(returned.code) || "Попробуйте ещё раз."}
          </Notice>
        ) : null}
        {start.isError ? <Notice kind="error">{errorMessage(start.error)}</Notice> : null}

        <WhereItSearches />

        <div>
          <h2 className={settingsStyles.sectionTitle}>Ваши аккаунты</h2>
          <p className={settingsStyles.sectionText}>
            Подключите рабочие аккаунты — и kronto будет отвечать ещё и по документам, которые
            доступны лично вам. Другие сотрудники их не увидят.
          </p>
        </div>
        {mine.isPending ? (
          <PageSpinner />
        ) : mine.isError ? (
          <Notice kind="error">{errorMessage(mine.error)}</Notice>
        ) : mine.data.length === 0 ? (
          <EmptyState icon={<Link2 size={32} aria-hidden />} title="Подключать пока нечего">
            <p>
              {isAdmin(me) ? (
                <>
                  Добавьте Битрикс24 или Яндекс 360 в разделе{" "}
                  <Link to="/admin/sources/connections">«Источники → Подключения»</Link> — тогда
                  сотрудники смогут подключить свои аккаунты здесь.
                </>
              ) : (
                "Администратор ещё не настроил источники, которые подключаются личным аккаунтом."
              )}
            </p>
          </EmptyState>
        ) : (
          <ul className={styles.cards}>
            {mine.data.map((item) => (
              <li key={item.id} className={styles.card}>
                <div className={styles.cardHead}>
                  <div>
                    <p className={styles.cardTitle}>{item.name}</p>
                    {item.grant_error_code ? (
                      <p
                        style={{ color: "var(--error)", fontSize: "var(--fs-small)", marginTop: 4 }}
                      >
                        {describeConnectCode(item.grant_error_code)}
                      </p>
                    ) : null}
                  </div>
                  <GrantBadge item={item} />
                </div>
                {!item.oauth && item.credential_fields.length > 0 ? (
                  <CredentialsCard item={item} onDisconnect={() => setDisconnecting(item)} />
                ) : (
                  <div className={pageStyles.row}>
                    {item.oauth ? (
                      <Button
                        size="sm"
                        variant={item.grant_status === "active" ? "ghost" : "dark"}
                        busy={start.isPending && start.variables === item.id}
                        disabled={start.isPending}
                        onClick={() => start.mutate(item.id)}
                      >
                        {item.grant_status === "active" ? "Подключить заново" : "Подключить"}
                      </Button>
                    ) : (
                      <span className="muted" style={{ fontSize: "var(--fs-small)" }}>
                        Подключается через администратора.
                      </span>
                    )}
                    {item.grant_status ? (
                      <Button variant="link" size="sm" onClick={() => setDisconnecting(item)}>
                        Отключить
                      </Button>
                    ) : null}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
      <ConfirmDialog
        open={disconnecting !== null}
        onOpenChange={(open) => !open && setDisconnecting(null)}
        title="Отключить аккаунт?"
        description="kronto перестанет видеть ваши документы из этого источника и уберёт их из ответов."
        confirmLabel="Отключить"
        onConfirm={async () => {
          if (!disconnecting) return;
          await unwrap(
            api.DELETE("/api/v1/connectors/{connector_id}/mine", {
              params: { path: { connector_id: disconnecting.id } },
            }),
          );
          await queryClient.invalidateQueries({ queryKey: ["connectors"] });
        }}
      />
    </>
  );
}

/** Подписи кодов там, где общий текст из codes.ts говорит не о том. */
function describeConnectCode(code: string | null | undefined): string {
  if (code === "timeout") return "Источник не ответил вовремя — попробуйте позже";
  return describeCode(code);
}

function connectError(error: unknown): string {
  // Текст 422 — служебный («…: auth_failed»), человеку — подпись кода.
  if (error instanceof ApiError && error.code) return describeConnectCode(error.code);
  return errorMessage(error);
}

/**
 * Источник без OAuth (Kaiten, Nextcloud и другие WebDAV-диски): сотрудник
 * сам вводит токен или логин с паролем приложения. Сервер сразу проверяет
 * их в источнике; сохранённые значения не возвращаются — «Изменить данные»
 * открывает пустую форму.
 */
function CredentialsCard({ item, onDisconnect }: { item: Mine; onDisconnect: () => void }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [values, setValues] = useState<Record<string, string>>({});
  const fields = item.credential_fields;
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        api.PUT("/api/v1/connectors/{connector_id}/mine", {
          params: { path: { connector_id: item.id } },
          // Пароли не обрезаем: пробел может быть его частью.
          body: { credentials: filled(values, false) },
        }),
      ),
    onSuccess: async () => {
      setValues({});
      setEditing(false);
      await queryClient.invalidateQueries({ queryKey: ["connectors"] });
    },
    onError: () => {
      // Секреты вводятся заново; логин и прочее остаются.
      setValues((current) =>
        Object.fromEntries(
          Object.entries(current).filter(
            ([name]) => !fields.some((field) => field.name === name && field.secret),
          ),
        ),
      );
    },
  });
  const connected = Boolean(item.grant_status);
  const formOpen = !connected || editing;
  const ready = fields.every((field) => !field.required || values[field.name]?.trim());

  return (
    <>
      {save.isSuccess && !formOpen ? (
        <Notice kind="ok" title="Аккаунт подключён">
          Документы появятся в ответах после ближайшей синхронизации — обычно в течение часа.
        </Notice>
      ) : null}
      {formOpen ? (
        <form
          className={settingsStyles.form}
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate();
          }}
        >
          <p className="muted" style={{ fontSize: "var(--fs-small)" }}>
            {connected
              ? "Сохранённые данные не показываются — впишите новые."
              : "kronto сразу проверит данные в источнике и сохранит их зашифрованными."}
          </p>
          {save.isError ? <Notice kind="error">{connectError(save.error)}</Notice> : null}
          <SecretInputs
            fields={fields}
            values={values}
            onChange={(name, value) => setValues((current) => ({ ...current, [name]: value }))}
            replacing={false}
          />
          <div className={pageStyles.row}>
            <Button type="submit" size="sm" busy={save.isPending} disabled={!ready}>
              {connected ? "Сохранить" : "Подключить"}
            </Button>
            {editing ? (
              <Button
                variant="ghost"
                size="sm"
                disabled={save.isPending}
                onClick={() => {
                  setEditing(false);
                  setValues({});
                  save.reset();
                }}
              >
                Отмена
              </Button>
            ) : null}
          </div>
        </form>
      ) : (
        <div className={pageStyles.row}>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              save.reset();
              setEditing(true);
            }}
          >
            Изменить данные
          </Button>
          <Button variant="link" size="sm" onClick={onDisconnect}>
            Отключить
          </Button>
        </div>
      )}
    </>
  );
}

function GrantBadge({ item }: { item: Mine }) {
  switch (item.grant_status) {
    case "active":
      return <Badge tone="ok">подключён</Badge>;
    case "expired":
      return <Badge tone="warn">доступ истёк</Badge>;
    case "revoked":
      return <Badge tone="error">доступ отозван</Badge>;
    default:
      return <Badge>не подключён</Badge>;
  }
}

/** «Где ищет ассистент»: то же правило видимости, что у поиска. */
function WhereItSearches() {
  const sources = useQuery({
    queryKey: ["connectors", "sources"],
    queryFn: () => unwrap(api.GET("/api/v1/sources/mine")),
  });
  return (
    <Section
      title="Где ищет ассистент"
      description="Документы и источники, по которым kronto отвечает именно вам. Папки с доступом по отделам видны только их сотрудникам."
    >
      {sources.isPending ? (
        <PageSpinner />
      ) : sources.isError ? (
        <Notice kind="error">{errorMessage(sources.error)}</Notice>
      ) : (
        <SourcesList data={sources.data} />
      )}
    </Section>
  );
}

function SourcesList({ data }: { data: Sources }) {
  if (!data.files.length && !data.connectors.length) {
    return (
      <p className="muted">
        Пока ассистенту не из чего отвечать: администратор ещё не загрузил документы и не подключил
        источники.
      </p>
    );
  }
  return (
    <ul className={settingsStyles.list}>
      {data.files.map((group) => (
        <li key={group.folder_id ?? "root"} className={settingsStyles.item}>
          <div className={settingsStyles.itemMain}>
            <span className={settingsStyles.icon} aria-hidden>
              {group.restricted ? <FolderLock size={18} /> : <FolderClosed size={18} />}
            </span>
            <div className={settingsStyles.itemText}>
              <span className={settingsStyles.itemTitle}>
                {group.name}
                {group.restricted ? <Badge>для вашего отдела</Badge> : null}
              </span>
              <span className={settingsStyles.meta}>
                {formatNumber(group.documents)}{" "}
                {plural(group.documents, "документ", "документа", "документов")}
              </span>
            </div>
          </div>
        </li>
      ))}
      {data.connectors.map((item) => (
        <li key={item.id} className={settingsStyles.item}>
          <div className={settingsStyles.itemMain}>
            <span className={settingsStyles.icon} aria-hidden>
              <Plug size={18} />
            </span>
            <div className={settingsStyles.itemText}>
              <span className={settingsStyles.itemTitle}>{item.name}</span>
              <span className={settingsStyles.meta}>
                {item.mode === "per_user" ? "ваш личный аккаунт" : "подключено компанией"}
              </span>
            </div>
          </div>
          <SourceBadge item={item} />
        </li>
      ))}
    </ul>
  );
}

function SourceBadge({ item }: { item: Sources["connectors"][number] }) {
  if (!item.working) return <Badge tone="warn">временно не работает</Badge>;
  if (item.mode !== "per_user") return <Badge tone="ok">ищет</Badge>;
  switch (item.grant_status) {
    case "active":
      return <Badge tone="ok">ищет по вашим файлам</Badge>;
    case "expired":
      return <Badge tone="warn">доступ истёк</Badge>;
    case "revoked":
      return <Badge tone="error">доступ отозван</Badge>;
    default:
      return <Badge>не подключён</Badge>;
  }
}
