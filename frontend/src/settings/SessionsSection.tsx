import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LogOut } from "lucide-react";
import { useState } from "react";

import { ConfirmDialog } from "../admin/common";
import { api, unwrap } from "../api/client";
import { errorMessage } from "../api/errors";
import { useAuth } from "../auth/context";
import { formatDateTime, formatRelative } from "../lib/format";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Notice } from "../ui/Notice";
import { SkeletonList } from "../ui/Skeleton";
import { Table } from "../ui/Table";
import tableStyles from "../ui/Table.module.css";
import { useToast } from "../ui/useToast";
import { Section } from "./common";
import { SESSIONS_KEY } from "./keys";

/** Где открыта учётка (ТЗ §3): выйти на одном устройстве или везде. */
export function SessionsSection() {
  const { logout } = useAuth();
  const queryClient = useQueryClient();
  const toast = useToast();
  const [everywhere, setEverywhere] = useState(false);
  const sessions = useQuery({
    queryKey: SESSIONS_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/account/sessions")),
    // Это устройство — первым: его ищут глазами в первую очередь.
    select: (list) => [...list].sort((a, b) => Number(b.current) - Number(a.current)),
  });
  const end = useMutation({
    mutationFn: (id: string) =>
      unwrap(
        api.POST("/api/v1/account/sessions/{session_id}/end", {
          params: { path: { session_id: id } },
        }),
      ),
    onSuccess: async () => {
      // Выданный устройству access-токен доживает свой срок — до 15 минут.
      toast.show("Сеанс завершён. Устройство потеряет доступ в течение 15 минут.");
      await queryClient.invalidateQueries({ queryKey: SESSIONS_KEY });
    },
  });

  return (
    <Section
      title="Сеансы"
      description="Устройства, на которых открыта ваша учётная запись. Не узнаёте устройство — завершите сеанс и смените пароль."
      aside={
        <Button variant="ghost" size="sm" onClick={() => setEverywhere(true)}>
          <LogOut size={16} aria-hidden /> Выйти на всех устройствах
        </Button>
      }
    >
      {end.isError ? <Notice kind="error">{errorMessage(end.error)}</Notice> : null}
      {sessions.isPending ? (
        <SkeletonList rows={2} label="Загрузка сеансов" />
      ) : sessions.isError ? (
        <Notice kind="error">{errorMessage(sessions.error)}</Notice>
      ) : (
        <Table label="Сеансы">
          <thead>
            <tr>
              <th>Устройство</th>
              <th>IP-адрес</th>
              <th>Начат</th>
              <th>Активность</th>
              <th className={tableStyles.actions}>
                <span className="visually-hidden">Действия</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {sessions.data.map((item) => (
              <tr key={item.id}>
                <td>
                  <span style={{ fontWeight: 500 }}>{item.device ?? "Неизвестное устройство"}</span>{" "}
                  {item.current ? <Badge tone="accent">Это устройство</Badge> : null}
                </td>
                <td className="num">{item.ip ?? "—"}</td>
                <td className={tableStyles.nowrap}>{formatDateTime(item.started_at)}</td>
                <td className={tableStyles.nowrap} title={formatDateTime(item.last_active_at)}>
                  {item.current ? "сейчас" : formatRelative(item.last_active_at)}
                </td>
                <td className={tableStyles.actions}>
                  {item.current ? null : (
                    <Button
                      variant="ghost"
                      size="xs"
                      busy={end.isPending && end.variables === item.id}
                      disabled={end.isPending}
                      aria-label={`Завершить сеанс: ${item.device ?? "неизвестное устройство"}`}
                      onClick={() => end.mutate(item.id)}
                    >
                      Завершить
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      <ConfirmDialog
        open={everywhere}
        onOpenChange={setEverywhere}
        title="Выйти на всех устройствах?"
        description="Завершатся все сеансы, включая этот. Войти снова можно с паролем и вторым фактором; «запомненные» устройства тоже попросят его."
        confirmLabel="Выйти везде"
        onConfirm={async () => {
          await unwrap(api.POST("/api/v1/auth/logout-all"));
          await logout();
        }}
      />
    </Section>
  );
}
