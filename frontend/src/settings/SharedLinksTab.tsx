import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";

import { api, unwrap } from "../api/client";
import { errorMessage } from "../api/errors";
import { renewShare, revokeShare, type SharedLink } from "../chat/api";
import { CONVERSATIONS_KEY, SHARED_LINKS_KEY } from "../chat/keys";
import { formatDate, formatDateTime } from "../lib/format";
import { useDocumentTitle } from "../lib/title";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Notice } from "../ui/Notice";
import { SkeletonList } from "../ui/Skeleton";
import { Table } from "../ui/Table";
import tableStyles from "../ui/Table.module.css";
import { useToast } from "../ui/useToast";
import { Section } from "./common";
import styles from "./Settings.module.css";

/**
 * «Мои общие ссылки» (ТЗ §6): диалоги, которыми вы поделились с коллегами,
 * и до какого числа ссылка открывается. Истёкшую видно здесь же: её можно
 * продлить — срок снова от сегодня (30 дней), ссылка та же. Скопировать
 * ссылку — в самом диалоге, «Поделиться».
 */
export function SharedLinksTab() {
  useDocumentTitle("Общие ссылки");
  const queryClient = useQueryClient();
  const toast = useToast();
  const links = useQuery({
    queryKey: SHARED_LINKS_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/conversations/shares")),
  });
  // Карточка диалога держит ссылку и срок — её тоже перечитать.
  const refresh = () => queryClient.invalidateQueries({ queryKey: CONVERSATIONS_KEY });
  const renew = useMutation({
    mutationFn: (id: string) => renewShare(id),
    onSuccess: async () => {
      toast.show("Ссылка продлена");
      await refresh();
    },
  });
  const revoke = useMutation({
    mutationFn: (id: string) => revokeShare(id),
    onSuccess: async () => {
      toast.show("Доступ по ссылке закрыт");
      await refresh();
    },
  });
  const failure = renew.error ?? revoke.error;
  const busy = renew.isPending || revoke.isPending;

  return (
    <div className={styles.stack}>
      <Section
        title="Мои общие ссылки"
        description="Диалоги, которыми вы поделились с коллегами по компании. У ссылки есть срок; «Продлить» отсчитывает его заново от сегодня, ссылка остаётся прежней."
      >
        {failure ? <Notice kind="error">{errorMessage(failure)}</Notice> : null}
        {links.isPending ? (
          <SkeletonList rows={2} label="Загружаем ссылки" />
        ) : links.isError ? (
          <Notice kind="error">{errorMessage(links.error)}</Notice>
        ) : links.data.items.length === 0 ? (
          <p className={styles.lead}>
            Вы ещё не делились диалогами. Откройте диалог и нажмите «Поделиться».
          </p>
        ) : (
          <Table label="Общие ссылки">
            <thead>
              <tr>
                <th>Диалог</th>
                <th>Снимок</th>
                <th>Срок</th>
                <th className={tableStyles.actions}>
                  <span className="visually-hidden">Действия</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {links.data.items.map((link) => (
                <tr key={link.id}>
                  <td>
                    <Link to={`/c/${link.id}`}>{link.title}</Link>
                  </td>
                  <td className={tableStyles.nowrap}>{formatDateTime(link.shared_at)}</td>
                  <td className={tableStyles.nowrap}>
                    <Term link={link} />
                  </td>
                  <td className={tableStyles.actions}>
                    <Button
                      variant="ghost"
                      size="xs"
                      busy={renew.isPending && renew.variables === link.id}
                      disabled={busy}
                      aria-label={`Продлить: ${link.title}`}
                      onClick={() => renew.mutate(link.id)}
                    >
                      Продлить
                    </Button>
                    <Button
                      variant="ghost"
                      size="xs"
                      busy={revoke.isPending && revoke.variables === link.id}
                      disabled={busy}
                      aria-label={`Отключить: ${link.title}`}
                      onClick={() => revoke.mutate(link.id)}
                    >
                      Отключить
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Section>
    </div>
  );
}

function Term({ link }: { link: SharedLink }) {
  if (link.expired) {
    return <Badge tone="warn">истекла {formatDate(link.expires_at)}</Badge>;
  }
  return <>до {formatDate(link.expires_at)}</>;
}
