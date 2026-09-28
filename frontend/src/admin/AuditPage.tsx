import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { ScrollText } from "lucide-react";
import { useMemo } from "react";

import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { formatDateTime } from "../lib/format";
import { Button } from "../ui/Button";
import { Notice } from "../ui/Notice";
import { EmptyState, Page, PageHeader } from "../ui/Page";
import { PageSpinner } from "../ui/Spinner";
import { Table } from "../ui/Table";
import tableStyles from "../ui/Table.module.css";
import styles from "./Admin.module.css";

type Event = Schemas["AuditEventResponse"];
const PAGE = 50;

const ACTIONS: Record<string, string> = {
  "auth.login.succeeded": "Вход",
  "auth.login.failed": "Неудачный вход",
  "auth.logout": "Выход",
  "auth.logout_everywhere": "Выход на всех устройствах",
  "auth.password.changed": "Смена пароля",
  "auth.refresh.reuse_detected": "Повторное использование токена — сеансы отозваны",
  "user.created": "Сотрудник добавлен",
  "user.updated": "Сотрудник изменён",
  "user.password_reset": "Выдан временный пароль",
  "material.created": "Документ добавлен",
  "material.updated": "Документ изменён",
  "material.deleted": "Документ удалён",
  "glossary.created": "Термин добавлен",
  "glossary.updated": "Термин изменён",
  "glossary.deleted": "Термин удалён",
  "gap.status_changed": "Статус пробела изменён",
  "connector.created": "Подключение создано",
  "connector.updated": "Подключение изменено",
  "connector.deleted": "Подключение удалено",
  "connector.credentials_set": "Ключи подключения заданы",
  "connector.sync_requested": "Запрошена синхронизация",
  "connector.stopped": "Подключение остановлено",
  "connector.grant_set": "Сотрудник подключил источник",
  "connector.grant_expired": "Доступ сотрудника к источнику истёк",
  "connector.grant_revoked": "Сотрудник отключил источник",
  "connector.oauth_failed": "Ошибка подключения источника",
  "credits.warning": "Лимит вопросов почти исчерпан",
  "credits.exhausted": "Лимит вопросов исчерпан",
  "tenant.created": "Компания создана",
  "tenant.seats_changed": "Изменено число мест",
  "tenant.suspended": "Доступ компании приостановлен",
  "tenant.resumed": "Доступ компании возобновлён",
  "tenant.not_found_mode_changed": "Изменён режим ответов без документов",
};

function details(event: Event): string {
  const entries = Object.entries(event.details).filter(
    ([, value]) => value !== null && value !== "",
  );
  return entries
    .slice(0, 4)
    .map(([key, value]) => {
      const text =
        typeof value === "string" || typeof value === "number" || typeof value === "boolean"
          ? String(value)
          : JSON.stringify(value);
      return `${key}: ${text}`;
    })
    .join(" · ");
}

export function AuditPage() {
  const users = useQuery({ queryKey: ["users"], queryFn: () => unwrap(api.GET("/api/v1/users")) });
  const events = useInfiniteQuery({
    queryKey: ["audit"],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/api/v1/audit", {
          params: { query: { limit: PAGE, ...(pageParam ? { before: pageParam } : {}) } },
        }),
      ),
    getNextPageParam: (last) => (last.length === PAGE ? (last.at(-1)?.created_at ?? null) : null),
  });
  const emails = useMemo(
    () => new Map((users.data ?? []).map((u) => [u.id, u.email])),
    [users.data],
  );
  const rows = events.data?.pages.flat() ?? [];

  return (
    <Page>
      <PageHeader
        label="управление"
        title="Журнал действий"
        description="Входы, изменения документов, сотрудников и подключений. Записи хранятся год и не редактируются."
      />
      {events.isPending ? (
        <PageSpinner />
      ) : events.isError ? (
        <Notice kind="error">{errorMessage(events.error)}</Notice>
      ) : rows.length === 0 ? (
        <EmptyState icon={<ScrollText size={32} aria-hidden />} title="Записей пока нет" />
      ) : (
        <>
          <Table label="Журнал действий">
            <thead>
              <tr>
                <th>Когда</th>
                <th>Событие</th>
                <th>Кто</th>
                <th>Подробности</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((event) => (
                <tr key={event.id}>
                  <td className={`${tableStyles.nowrap} num`}>
                    {formatDateTime(event.created_at)}
                  </td>
                  <td>
                    {ACTIONS[event.action] ?? event.action}
                    {event.ip ? <span className={tableStyles.sub}>IP {event.ip}</span> : null}
                  </td>
                  <td>
                    {event.actor_user_id
                      ? (emails.get(event.actor_user_id) ?? "сотрудник удалён")
                      : "система"}
                  </td>
                  <td>
                    <span className={styles.details}>{details(event)}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
          {events.hasNextPage ? (
            <div style={{ marginTop: 16 }}>
              <Button
                variant="ghost"
                size="sm"
                busy={events.isFetchingNextPage}
                onClick={() => void events.fetchNextPage()}
              >
                Показать ещё
              </Button>
            </div>
          ) : null}
        </>
      )}
    </Page>
  );
}
