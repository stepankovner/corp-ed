import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Bell,
  BellOff,
  ChartColumn,
  Coins,
  Gauge,
  HandCoins,
  Mail,
  Network,
  OctagonAlert,
  Unplug,
  UserPlus,
  type LucideIcon,
} from "lucide-react";
import { useId, useRef, type ReactNode } from "react";
import { Link } from "react-router";

import { api, unwrap, type Schemas } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { isAdmin, needsStrongFactor, useMe } from "../auth/context";
import { formatDateTime, formatRelative, plural } from "../lib/format";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "../ui/DropdownMenu";
import { IconButton } from "../ui/IconButton";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/useToast";
import styles from "./NotificationBell.module.css";

type Notifications = Schemas["NotificationsResponse"];
type Notification = Notifications["items"][number];
type Tone = "error" | "warn" | "accent" | "muted";

const LIST_KEY = ["notifications", "list"] as const;
const READ_KEY = ["notifications", "read"] as const;
/** Опрос, пока вкладка на экране; в фоне react-query не опрашивает. */
const POLL_MS = 60_000;

const KINDS: Record<Notification["kind"], { icon: LucideIcon; tone: Tone }> = {
  connector_stopped: { icon: Unplug, tone: "error" },
  credits_warning: { icon: Gauge, tone: "warn" },
  credits_exhausted: { icon: OctagonAlert, tone: "error" },
  credits_added: { icon: Coins, tone: "accent" },
  credits_topup_requested: { icon: HandCoins, tone: "warn" },
  join_request: { icon: UserPlus, tone: "accent" },
  department_request: { icon: Network, tone: "accent" },
  department_confirmed: { icon: Network, tone: "muted" },
  department_rejected: { icon: Network, tone: "warn" },
  weekly_digest: { icon: ChartColumn, tone: "muted" },
};

/** Ссылка уведомления — путь сайта («/admin/…»); чужие адреса не открываем. */
function sitePath(link: string | null): string | null {
  return link?.startsWith("/") && !link.startsWith("//") ? link : null;
}

/** 403 (no_company, нет надёжного входа) — колокольчика нет. */
function forbidden(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403;
}

/** Отметка «прочитано» до ответа сервера: ids нет — все. */
function markedRead(data: Notifications, ids: string[] | null): Notifications {
  const hit = (item: Notification) => ids === null || ids.includes(item.id);
  const fresh = data.items.filter((item) => !item.read && hit(item)).length;
  return {
    items: data.items.map((item) => (hit(item) ? { ...item, read: true } : item)),
    unread: ids === null ? 0 : Math.max(0, data.unread - fresh),
  };
}

/**
 * Колокольчик (ТЗ §8) — в шапке боковой панели и в верхней полосе телефона
 * (compact). Уведомления — у участника компании: без неё или без надёжного
 * входа сервер ответит 403, и колокольчика нет.
 */
export function NotificationBell({ compact = false }: { compact?: boolean }) {
  const me = useMe();
  if (me.company === null || needsStrongFactor(me)) return null;
  return <BellMenu compact={compact} admin={isAdmin(me)} />;
}

function BellMenu({ compact, admin }: { compact: boolean; admin: boolean }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const content = useRef<HTMLDivElement>(null);
  const notifications = useQuery({
    queryKey: LIST_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/notifications")),
    // Отказ (403) не пройдёт сам — не опрашиваем, пока не сменится компания или защита.
    refetchInterval: (query) => (forbidden(query.state.error) ? false : POLL_MS),
    refetchOnWindowFocus: true,
  });
  const markRead = useMutation({
    mutationKey: READ_KEY,
    mutationFn: (ids: string[] | null) =>
      unwrap(api.POST("/api/v1/notifications/read", { body: ids ? { ids } : {} })),
    onMutate: async (ids) => {
      await queryClient.cancelQueries({ queryKey: LIST_KEY });
      queryClient.setQueryData<Notifications>(LIST_KEY, (old) => old && markedRead(old, ids));
    },
    onSuccess: (data) => {
      // Следующая отметка уже в пути — её не затираем, ответ придёт с ней.
      if (queryClient.isMutating({ mutationKey: READ_KEY }) <= 1) {
        queryClient.setQueryData(LIST_KEY, data);
      }
    },
    onError: (error) => {
      toast.show(errorMessage(error), { tone: "error" });
      void queryClient.invalidateQueries({ queryKey: LIST_KEY });
    },
  });

  if (forbidden(notifications.error)) return null;

  const data = notifications.data;
  const unread = data?.unread ?? 0;
  const label = unread
    ? `Уведомления: ${unread} ${plural(unread, "непрочитанное", "непрочитанных", "непрочитанных")}`
    : "Уведомления";

  function readAll() {
    // Кнопка сейчас исчезнет — фокус остаётся в панели, стрелки снова ведут к пунктам.
    content.current?.focus();
    markRead.mutate(null);
  }

  let body: ReactNode;
  if (data?.items.length) {
    body = data.items.map((item) => (
      <Entry
        key={item.id}
        item={item}
        onOpen={() => {
          if (!item.read) markRead.mutate([item.id]);
        }}
      />
    ));
  } else if (data) {
    body = (
      <div className={styles.state}>
        <BellOff size={24} aria-hidden />
        <p>
          {admin
            ? "Пока ничего — здесь появятся остановленные подключения, кредиты и заявки на вступление"
            : "Пока ничего нет"}
        </p>
      </div>
    );
  } else if (notifications.isError) {
    body = (
      <p className={styles.state} role="alert">
        {errorMessage(notifications.error)}
      </p>
    );
  } else {
    body = (
      <div className={styles.state}>
        <Spinner size={20} label="Загрузка" />
      </div>
    );
  }

  return (
    <DropdownMenu
      onOpenChange={(open) => {
        if (open && notifications.isStale) void notifications.refetch();
      }}
    >
      <DropdownMenuTrigger asChild>
        <IconButton
          label={label}
          tooltip={!compact}
          tooltipSide="bottom"
          className={[styles.trigger, compact ? styles.compact : styles.inSidebar].join(" ")}
        >
          <Bell size={20} aria-hidden />
          {unread ? (
            <span className={styles.count} aria-hidden>
              {unread > 9 ? "9+" : unread}
            </span>
          ) : null}
        </IconButton>
      </DropdownMenuTrigger>
      <DropdownMenuContent ref={content} align={compact ? "end" : "start"} className={styles.panel}>
        <div className={styles.head}>
          <span className={styles.heading}>Уведомления</span>
          {unread ? (
            <DropdownMenuItem
              className={styles.readAll}
              onSelect={(event) => {
                event.preventDefault();
                readAll();
              }}
            >
              Прочитать все
            </DropdownMenuItem>
          ) : null}
        </div>
        <div className={styles.list}>{body}</div>
        {admin ? (
          <div className={styles.foot}>
            <DropdownMenuItem asChild>
              <Link to="/settings/notifications">
                <Mail size={16} aria-hidden /> Настроить письма
              </Link>
            </DropdownMenuItem>
          </div>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * Пункт панели: значок вида, заголовок (жирный — не прочитано), начало
 * текста, время. Имя пункта — заголовок, текст и время — описание: иначе
 * скринридер читал бы всё одной склеенной строкой.
 */
function Entry({ item, onOpen }: { item: Notification; onOpen: () => void }) {
  const id = useId();
  const { icon: Icon, tone } = KINDS[item.kind];
  const path = sitePath(item.link);
  const a11y = {
    "aria-label": item.read ? item.title : `Новое: ${item.title}`,
    "aria-describedby": [item.body ? `${id}-body` : "", `${id}-time`].filter(Boolean).join(" "),
  };
  const className = [styles.entry, item.read ? "" : styles.unread].join(" ");
  const inner = (
    <>
      <span className={[styles.kind, styles[tone]].join(" ")} aria-hidden>
        <Icon size={16} />
      </span>
      <span className={styles.text}>
        <span className={styles.title}>{item.title}</span>
        {item.body ? (
          <span className={styles.body} id={`${id}-body`}>
            {item.body}
          </span>
        ) : null}
        <time
          className={styles.time}
          id={`${id}-time`}
          dateTime={item.created_at}
          title={formatDateTime(item.created_at)}
        >
          {formatRelative(item.created_at)}
        </time>
      </span>
      {item.read ? null : <span className={styles.dot} aria-hidden />}
    </>
  );
  return path ? (
    <DropdownMenuItem asChild className={className} onSelect={onOpen}>
      <Link to={path} {...a11y}>
        {inner}
      </Link>
    </DropdownMenuItem>
  ) : (
    <DropdownMenuItem className={className} onSelect={onOpen} {...a11y}>
      {inner}
    </DropdownMenuItem>
  );
}
