import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, Check, Copy, Link2, Link2Off, RefreshCw } from "lucide-react";
import { useState } from "react";

import { api, unwrap } from "../api/client";
import { errorMessage } from "../api/errors";
import { useCompany } from "../auth/context";
import { formatDate, formatDateTime } from "../lib/format";
import { Button } from "../ui/Button";
import { Modal } from "../ui/Modal";
import { Notice } from "../ui/Notice";
import { useToast } from "../ui/useToast";
import { renewShare, revokeShare, shareUrl, type Conversation } from "./api";
import styles from "./Chat.module.css";
import { CONVERSATION_LISTS_KEY, conversationKey, SHARED_LINKS_KEY } from "./keys";

/**
 * «Поделиться диалогом» (ТЗ §6): ссылку откроют только коллеги по
 * компании. По ссылке — диалог таким, каким он был в момент «Создать» или
 * «Обновить»; оценки и комментарии автора не видны. Срок ссылки (30 дней)
 * отсчитывается от «Создать», «Обновить» или «Продлить»; истёкшая не
 * открывается, пока её не продлят.
 */
export function ShareDialog({
  conversation,
  onClose,
}: {
  conversation: Conversation;
  onClose: () => void;
}) {
  const company = useCompany();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [copied, setCopied] = useState(false);
  const key = conversationKey(conversation.id);
  const path = { params: { path: { conversation_id: conversation.id } } };

  function store(share: Conversation["share"]) {
    queryClient.setQueryData<Conversation>(key, (old) =>
      old ? { ...old, share, shared: share !== null } : old,
    );
    void queryClient.invalidateQueries({ queryKey: CONVERSATION_LISTS_KEY });
    void queryClient.invalidateQueries({ queryKey: SHARED_LINKS_KEY });
  }

  const create = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/conversations/{conversation_id}/share", path)),
    onSuccess: (share) => store(share),
  });
  const renew = useMutation({
    mutationFn: () => renewShare(conversation.id),
    onSuccess: (share) => store(share),
  });
  const revoke = useMutation({
    mutationFn: () => revokeShare(conversation.id),
    onSuccess: () => {
      store(null);
      toast.show("Доступ по ссылке закрыт");
    },
  });
  const share = conversation.share;
  const url = share ? shareUrl(share.token) : null;
  const failure = create.error ?? renew.error ?? revoke.error;

  async function copy() {
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      toast.show("Ссылка скопирована");
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      toast.show("Скопируйте ссылку вручную: браузер не дал доступа к буферу обмена", {
        tone: "error",
      });
    }
  }

  return (
    <Modal
      open
      onOpenChange={(open) => !open && onClose()}
      title="Поделиться диалогом"
      description={`Ссылку откроют только коллеги по «${company.name}». Им будет виден диалог таким, какой он сейчас; ваши оценки и комментарии — нет.`}
    >
      <div className={styles.share}>
        {failure ? <Notice kind="error">{errorMessage(failure)}</Notice> : null}
        {url && share ? (
          <>
            <div className={styles.shareRow}>
              <label className="visually-hidden" htmlFor="share-link">
                Ссылка на диалог
              </label>
              <input
                id="share-link"
                className={styles.shareInput}
                readOnly
                value={url}
                onFocus={(e) => e.target.select()}
              />
              <Button size="sm" onClick={() => void copy()}>
                {copied ? <Check size={16} aria-hidden /> : <Copy size={16} aria-hidden />}{" "}
                Скопировать
              </Button>
            </div>
            {share.expired ? (
              <Notice kind="warn">
                Срок ссылки истёк {formatDate(share.expires_at)}: коллеги её не откроют. Продлите —
                ссылка останется прежней.
              </Notice>
            ) : (
              <p className="muted">Ссылка действует до {formatDate(share.expires_at)}.</p>
            )}
            <p className="muted">
              Снимок от {formatDateTime(share.shared_at)}. Новые сообщения появятся по ссылке после
              «Обновить».
            </p>
            <div className={styles.shareActions}>
              {share.expired ? (
                <Button
                  variant="ghost"
                  size="sm"
                  busy={renew.isPending}
                  onClick={() => renew.mutate()}
                >
                  <CalendarClock size={16} aria-hidden /> Продлить
                </Button>
              ) : null}
              <Button
                variant="ghost"
                size="sm"
                busy={create.isPending}
                onClick={() => create.mutate()}
              >
                <RefreshCw size={16} aria-hidden /> Обновить
              </Button>
              <Button
                variant="ghost"
                size="sm"
                busy={revoke.isPending}
                onClick={() => revoke.mutate()}
              >
                <Link2Off size={16} aria-hidden /> Закрыть доступ
              </Button>
            </div>
          </>
        ) : (
          <Button busy={create.isPending} onClick={() => create.mutate()}>
            <Link2 size={16} aria-hidden /> Создать ссылку
          </Button>
        )}
      </div>
    </Modal>
  );
}
