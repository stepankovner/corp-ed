import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link2 } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";

import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { isAdmin, useMe } from "../auth/context";
import { describeCode } from "../lib/codes";
import { useDocumentTitle } from "../lib/title";
import styles from "../admin/Admin.module.css";
import { ConfirmDialog } from "../admin/common";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Notice } from "../ui/Notice";
import { EmptyState, Page, PageHeader } from "../ui/Page";
import pageStyles from "../ui/Page.module.css";
import { PageSpinner } from "../ui/Spinner";

type Mine = Schemas["MyConnectorResponse"];

/**
 * Источники, которые сотрудник подключает своим аккаунтом (режим per_user):
 * Kronto видит в них только то, что доступно ему самому. Возврат с портала
 * приходит сюда с ?status=ok|error&connector_id=&error_code=.
 */
export function MySourcesPage() {
  useDocumentTitle("Мои источники");
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
    mutationFn: (id: string) =>
      unwrap(
        api.POST("/api/v1/connectors/{connector_id}/oauth/start", {
          params: { path: { connector_id: id } },
        }),
      ),
    onSuccess: ({ authorize_url }) => {
      window.location.assign(authorize_url);
    },
  });

  return (
    <Page>
      <PageHeader
        label="доступ"
        title="Мои источники"
        description="Подключите рабочие аккаунты — и Kronto будет отвечать ещё и по документам, которые доступны лично вам. Другие сотрудники их не увидят."
      />
      <div className={pageStyles.stack}>
        {returned.status === "ok" ? (
          <Notice kind="ok" title="Аккаунт подключён">
            Документы появятся в ответах после ближайшей синхронизации — обычно в течение часа.
          </Notice>
        ) : null}
        {returned.status === "error" ? (
          <Notice kind="error" title="Не удалось подключить">
            {describeCode(returned.code) || "Попробуйте ещё раз."}
          </Notice>
        ) : null}
        {start.isError ? <Notice kind="error">{errorMessage(start.error)}</Notice> : null}

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
                  <Link to="/admin/connectors">«Подключения»</Link> — тогда сотрудники смогут
                  подключить свои аккаунты здесь.
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
                        {describeCode(item.grant_error_code)}
                      </p>
                    ) : null}
                  </div>
                  <GrantBadge item={item} />
                </div>
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
              </li>
            ))}
          </ul>
        )}
      </div>
      <ConfirmDialog
        open={disconnecting !== null}
        onOpenChange={(open) => !open && setDisconnecting(null)}
        title="Отключить аккаунт?"
        description="Kronto перестанет видеть ваши документы из этого источника и уберёт их из ответов."
        confirmLabel="Отключить"
        onConfirm={async () => {
          if (!disconnecting) return;
          await unwrap(
            api.DELETE("/api/v1/connectors/{connector_id}/mine", {
              params: { path: { connector_id: disconnecting.id } },
            }),
          );
          await queryClient.invalidateQueries({ queryKey: ["connectors", "mine"] });
        }}
      />
    </Page>
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
