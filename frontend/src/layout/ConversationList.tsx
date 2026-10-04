import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { MoreHorizontal, Pencil, Pin, PinOff, Search, Trash2 } from "lucide-react";
import { useDeferredValue, useEffect, useState, type KeyboardEvent, type SubmitEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router";

import { api, unwrap } from "../api/client";
import { errorMessage } from "../api/errors";
import { ConfirmDialog } from "../admin/common";
import type { Summary } from "../chat/api";
import { CONVERSATION_LISTS_KEY, conversationKey, conversationListKey } from "../chat/keys";
import { useChat } from "../chat/store";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "../ui/DropdownMenu";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/useToast";
import styles from "./Sidebar.module.css";

const DAY = 86_400_000;

/** Группы по последней активности, как в ChatGPT. */
function groupOf(updatedAt: string, now: Date): string {
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const time = new Date(updatedAt).getTime();
  if (time >= today) return "Сегодня";
  if (time >= today - DAY) return "Вчера";
  if (time >= today - 7 * DAY) return "Последние 7 дней";
  if (time >= today - 30 * DAY) return "Последние 30 дней";
  return "Ранее";
}

/**
 * Диалоги человека в боковой панели (ТЗ §6): поиск по названиям и тексту,
 * закреплённые сверху, переименование и удаление. Видит только сам человек.
 */
export function ConversationList() {
  const [query, setQuery] = useState("");
  const deferred = useDeferredValue(query.trim());
  const list = useInfiniteQuery({
    queryKey: conversationListKey(deferred),
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/api/v1/conversations", {
          params: { query: { q: deferred || undefined, before: pageParam, limit: 50 } },
        }),
      ),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_before ?? undefined,
  });
  const items = list.data?.pages.flatMap((page) => page.items) ?? [];
  const pinned = items.filter((item) => item.pinned);
  const groups = new Map<string, Summary[]>();
  const now = new Date();
  for (const item of items.filter((entry) => !entry.pinned)) {
    const name = groupOf(item.updated_at, now);
    groups.set(name, [...(groups.get(name) ?? []), item]);
  }

  return (
    <section className={styles.dialogs} aria-label="Диалоги">
      <label className={styles.dialogSearch}>
        <Search size={16} aria-hidden />
        <span className="visually-hidden">Поиск по диалогам</span>
        <input
          type="search"
          placeholder="Поиск по диалогам"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </label>
      {list.isPending ? (
        <div className={styles.dialogsNote}>
          <Spinner size={16} />
        </div>
      ) : list.isError ? (
        <p className={styles.dialogsNote}>{errorMessage(list.error)}</p>
      ) : items.length === 0 ? (
        <p className={styles.dialogsNote}>
          {deferred ? "Ничего не нашли" : "Здесь появятся ваши диалоги"}
        </p>
      ) : (
        <>
          {pinned.length > 0 ? <Group title="Закреплённые" items={pinned} /> : null}
          {[...groups.entries()].map(([title, entries]) => (
            <Group key={title} title={title} items={entries} />
          ))}
          {list.hasNextPage ? (
            <button
              type="button"
              className={styles.more}
              disabled={list.isFetchingNextPage}
              onClick={() => void list.fetchNextPage()}
            >
              {list.isFetchingNextPage ? "Загружаю…" : "Показать ещё"}
            </button>
          ) : null}
        </>
      )}
    </section>
  );
}

function Group({ title, items }: { title: string; items: Summary[] }) {
  return (
    <div className={styles.dialogGroup}>
      <h2 className={`mono ${styles.dialogGroupTitle}`}>{title}</h2>
      <ul className={styles.list}>
        {items.map((item) => (
          <ConversationItem key={item.id} item={item} />
        ))}
      </ul>
    </div>
  );
}

function ConversationItem({ item }: { item: Summary }) {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToast();
  const { live } = useChat();
  const [renaming, setRenaming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const active = pathname === `/c/${item.id}`;

  const update = useMutation({
    mutationFn: (body: { title?: string; pinned?: boolean }) =>
      unwrap(
        api.PATCH("/api/v1/conversations/{conversation_id}", {
          params: { path: { conversation_id: item.id } },
          body,
        }),
      ),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: CONVERSATION_LISTS_KEY }),
        queryClient.invalidateQueries({ queryKey: conversationKey(item.id) }),
      ]);
    },
    onError: (err) => toast.show(errorMessage(err), { tone: "error" }),
  });

  return (
    <li className={styles.dialog}>
      {renaming ? (
        <RenameField
          id={item.id}
          title={item.title}
          onCancel={() => setRenaming(false)}
          onSave={(title) => {
            setRenaming(false);
            if (title !== item.title) update.mutate({ title });
          }}
        />
      ) : (
        <Link
          to={`/c/${item.id}`}
          aria-current={active ? "page" : undefined}
          className={[styles.item, styles.dialogLink, active ? styles.active : ""].join(" ")}
        >
          <span className={styles.label}>{item.title}</span>
          {live[item.id] ? <Spinner size={12} /> : null}
        </Link>
      )}
      {renaming ? null : (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className={styles.dialogMenu}
              aria-label={`Действия с диалогом «${item.title}»`}
            >
              <MoreHorizontal size={16} aria-hidden />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
            <DropdownMenuItem
              icon={<Pencil size={16} aria-hidden />}
              onSelect={() => setRenaming(true)}
            >
              Переименовать
            </DropdownMenuItem>
            <DropdownMenuItem
              icon={item.pinned ? <PinOff size={16} aria-hidden /> : <Pin size={16} aria-hidden />}
              onSelect={() => update.mutate({ pinned: !item.pinned })}
            >
              {item.pinned ? "Открепить" : "Закрепить"}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              icon={<Trash2 size={16} aria-hidden />}
              onSelect={() => setDeleting(true)}
            >
              Удалить
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
      <ConfirmDialog
        open={deleting}
        onOpenChange={setDeleting}
        title="Удалить диалог?"
        description={`«${item.title}» удалится со всеми версиями ответов и файлами. Вернуть его будет нельзя.`}
        confirmLabel="Удалить"
        onConfirm={async () => {
          await unwrap(
            api.DELETE("/api/v1/conversations/{conversation_id}", {
              params: { path: { conversation_id: item.id } },
            }),
          );
          if (active) void navigate("/");
          queryClient.removeQueries({ queryKey: conversationKey(item.id) });
          await queryClient.invalidateQueries({ queryKey: CONVERSATION_LISTS_KEY });
        }}
      />
    </li>
  );
}

function RenameField({
  id,
  title,
  onSave,
  onCancel,
}: {
  id: string;
  title: string;
  onSave: (title: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(title);
  const [input, setInput] = useState<HTMLInputElement | null>(null);
  useEffect(() => {
    input?.select();
  }, [input]);

  function submit(event: SubmitEvent) {
    event.preventDefault();
    const cleaned = value.split(/\s+/).filter(Boolean).join(" ");
    if (cleaned) onSave(cleaned);
    else onCancel();
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Escape") onCancel();
  }

  return (
    <form className={styles.rename} onSubmit={submit}>
      <label className="visually-hidden" htmlFor={`rename-${id}`}>
        Название диалога
      </label>
      <input
        ref={setInput}
        id={`rename-${id}`}
        value={value}
        maxLength={120}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        onBlur={() => {
          const cleaned = value.split(/\s+/).filter(Boolean).join(" ");
          if (cleaned && cleaned !== title) onSave(cleaned);
          else onCancel();
        }}
      />
    </form>
  );
}
