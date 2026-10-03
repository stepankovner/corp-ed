import { Copy, Download } from "lucide-react";
import { useState } from "react";

import { useMe } from "../auth/context";
import { formatDateTime } from "../lib/format";
import { Button } from "../ui/Button";
import { Checkbox } from "../ui/Field";
import { Modal } from "../ui/Modal";
import { useToast } from "../ui/useToast";
import styles from "./Settings.module.css";

/** Текст файла с кодами: человек найдёт его через полгода и поймёт, что это. */
function codesFile(codes: string[], email: string): string {
  return [
    `kronto — резервные коды для ${email}`,
    `Выпущены: ${formatDateTime(new Date().toISOString())}`,
    "",
    "Каждый код срабатывает один раз. Новые коды отменяют эти.",
    "",
    ...codes,
    "",
  ].join("\n");
}

function download(text: string, filename: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/**
 * Резервные коды показываются один раз (сервер хранит только хеши).
 * Закрыть окно можно, только отметив, что коды сохранены: без них
 * потерянный телефон — это потерянная учётка.
 */
export function BackupCodesDialog({ codes, onClose }: { codes: string[]; onClose: () => void }) {
  const me = useMe();
  const toast = useToast();
  const [saved, setSaved] = useState(false);
  const [nudge, setNudge] = useState(false);

  function close() {
    if (saved) onClose();
    else setNudge(true);
  }

  function copy() {
    void navigator.clipboard
      .writeText(codes.join("\n"))
      .then(() => toast.show("Коды скопированы"))
      .catch(() =>
        toast.show("Не удалось скопировать: браузер не дал доступа к буферу обмена", {
          tone: "error",
        }),
      );
  }

  return (
    <Modal
      open
      onOpenChange={(open) => !open && close()}
      title="Резервные коды"
      description="Сохраните их сейчас — больше мы их не покажем. Код пригодится, если телефон или ключ доступа потеряется; каждый срабатывает один раз."
      footer={
        <Button size="sm" onClick={close}>
          Готово
        </Button>
      }
    >
      <div className={styles.stack}>
        <ul className={styles.codes} aria-label="Резервные коды">
          {codes.map((code) => (
            <li key={code}>{code}</li>
          ))}
        </ul>
        <div className={styles.actions}>
          <Button variant="ghost" size="sm" onClick={copy}>
            <Copy size={16} aria-hidden /> Скопировать все
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => download(codesFile(codes, me.email), "kronto-backup-codes.txt")}
          >
            <Download size={16} aria-hidden /> Скачать .txt
          </Button>
        </div>
        <Checkbox
          label="Я сохранил коды"
          checked={saved}
          onChange={(e) => {
            setSaved(e.target.checked);
            if (e.target.checked) setNudge(false);
          }}
        />
        {nudge ? (
          <p className={styles.alert} role="alert">
            Сначала сохраните коды и отметьте это: показать их ещё раз мы не сможем.
          </p>
        ) : null}
      </div>
    </Modal>
  );
}
