import { useQueryClient } from "@tanstack/react-query";
import { KeyRound, Plus, Trash2 } from "lucide-react";
import { useId, useState, type SubmitEvent } from "react";

import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { useAuth } from "../auth/context";
import { formatDate, formatDateTime, formatRelative } from "../lib/format";
import { createPasskey, passkeysSupported, PasskeyError } from "../lib/webauthn";
import { Button } from "../ui/Button";
import { TextField } from "../ui/Field";
import { IconButton } from "../ui/IconButton";
import { Modal } from "../ui/Modal";
import { Notice } from "../ui/Notice";
import { useToast } from "../ui/useToast";
import { PasswordDialog, Section } from "./common";
import { SECURITY_KEY } from "./keys";
import styles from "./Settings.module.css";

type Security = Schemas["SecurityResponse"];
type Passkey = Schemas["PasskeyResponse"];

// Как в схеме бэкенда (PasskeyRegisterRequest.name).
const MAX_NAME = 100;
const DEFAULT_NAME = "Ключ доступа";

/**
 * Ключи доступа (WebAuthn): отпечаток, лицо, PIN-код устройства или
 * ключ безопасности. Ключ привязан к сайту — фишинговая копия его не
 * получит.
 */
export function PasskeysSection({
  security,
  onCodes,
}: {
  security: Security;
  onCodes: (codes: string[] | null) => void;
}) {
  const { reloadMe } = useAuth();
  const queryClient = useQueryClient();
  const toast = useToast();
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<Passkey | null>(null);
  const supported = passkeysSupported();
  const keys = security.passkeys;
  // Последний надёжный способ, а он обязателен: сервер удалить не даст.
  const locked = security.strong_required && !security.totp_enabled && keys.length === 1;

  async function refresh() {
    await Promise.all([reloadMe(), queryClient.invalidateQueries({ queryKey: SECURITY_KEY })]);
  }

  return (
    <Section
      title="Ключи доступа"
      description="Вход отпечатком, лицом, PIN-кодом устройства или ключом безопасности — без кодов. Ключ работает только на сайте kronto, поддельная страница его не получит."
      aside={
        supported ? (
          <Button
            size="sm"
            variant={keys.length ? "ghost" : "dark"}
            onClick={() => setAdding(true)}
          >
            <Plus size={16} aria-hidden /> Добавить ключ
          </Button>
        ) : null
      }
    >
      {supported ? null : (
        <Notice kind="info">
          Этот браузер не поддерживает ключи доступа. Добавить ключ можно в свежей версии Chrome,
          Safari, Edge или Firefox.
        </Notice>
      )}
      {keys.length ? (
        <ul className={styles.list} aria-label="Ваши ключи доступа">
          {keys.map((key) => (
            <li key={key.id} className={styles.item}>
              <div className={styles.itemMain}>
                <span className={styles.icon}>
                  <KeyRound size={18} aria-hidden />
                </span>
                <div className={styles.itemText}>
                  <span className={styles.itemTitle}>{key.name}</span>
                  <span className={styles.meta}>
                    <span>Добавлен {formatDate(key.created_at)}</span>
                    <span title={key.last_used_at ? formatDateTime(key.last_used_at) : undefined}>
                      {key.last_used_at
                        ? `Вход: ${formatRelative(key.last_used_at)}`
                        : "Для входа ещё не использовался"}
                    </span>
                  </span>
                </div>
              </div>
              <IconButton
                size="sm"
                label={`Удалить ключ «${key.name}»`}
                disabled={locked}
                onClick={() => setRemoving(key)}
              >
                <Trash2 size={16} aria-hidden />
              </IconButton>
            </li>
          ))}
        </ul>
      ) : (
        <p className={`muted ${styles.small}`}>Ключей пока нет.</p>
      )}
      {locked ? (
        <p className={`muted ${styles.small}`}>
          Это ваш единственный надёжный способ входа, а в ваших компаниях он обязателен. Чтобы
          удалить ключ, сначала добавьте другой или подключите приложение.
        </p>
      ) : null}
      {adding ? (
        <AddPasskeyDialog
          onClose={() => setAdding(false)}
          onAdded={async (key, codes) => {
            setAdding(false);
            onCodes(codes);
            toast.show(`Ключ «${key.name}» добавлен`);
            await refresh();
          }}
        />
      ) : null}
      {removing ? (
        <PasswordDialog
          title={`Удалить ключ «${removing.name}»?`}
          description="Войти этим ключом больше не получится. Если других надёжных способов нет, при входе снова будет код на почту, а резервные коды перестанут действовать."
          confirmLabel="Удалить"
          danger
          onClose={() => setRemoving(null)}
          onConfirm={async (password) => {
            await unwrap(
              api.POST("/api/v1/account/passkeys/{passkey_id}/delete", {
                params: { path: { passkey_id: removing.id } },
                body: { password },
              }),
            );
            setRemoving(null);
            toast.show("Ключ удалён");
            await refresh();
          }}
        />
      ) : null}
    </Section>
  );
}

function AddPasskeyDialog({
  onClose,
  onAdded,
}: {
  onClose: () => void;
  onAdded: (key: Passkey, codes: string[] | null) => Promise<void>;
}) {
  const formId = useId();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const { options, setup_token } = await unwrap(api.POST("/api/v1/account/passkeys/options"));
      const credential = await createPasskey(options);
      const created = await unwrap(
        api.POST("/api/v1/account/passkeys", {
          body: {
            setup_token,
            credential,
            name: name.split(/\s+/).filter(Boolean).join(" ") || DEFAULT_NAME,
          },
        }),
      );
      await onAdded(created.passkey, created.backup_codes);
    } catch (err) {
      setError(err instanceof PasskeyError ? err.message : errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      onOpenChange={(open) => !open && !busy && onClose()}
      title="Новый ключ доступа"
      description="Браузер предложит сохранить ключ: на этом устройстве, на телефоне или на ключе безопасности."
      footer={
        <>
          <Button variant="ghost" size="sm" onClick={onClose} disabled={busy}>
            Отмена
          </Button>
          <Button type="submit" form={formId} size="sm" busy={busy}>
            <KeyRound size={16} aria-hidden /> Создать ключ
          </Button>
        </>
      }
    >
      <form id={formId} className={styles.form} onSubmit={submit}>
        {error ? <Notice kind="error">{error}</Notice> : null}
        <TextField
          label="Название"
          optional
          maxLength={MAX_NAME}
          placeholder="Например, «Рабочий ноутбук»"
          hint="Чтобы отличать ключи друг от друга."
          value={name}
          onChange={(e) => setName(e.target.value)}
          autoFocus
        />
      </form>
    </Modal>
  );
}
