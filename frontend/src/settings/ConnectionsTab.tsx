import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderClosed, FolderLock, Link2, Plug } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";

import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { isAdmin, useMe } from "../auth/context";
import { describeCode } from "../lib/codes";
import { formatNumber, plural } from "../lib/format";
import { useDocumentTitle } from "../lib/title";
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
    <>
      <div className={settingsStyles.stack}>
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
