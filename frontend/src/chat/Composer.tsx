import { ArrowUp, FileText, Paperclip, ShieldCheck, Square, X } from "lucide-react";
import {
  useLayoutEffect,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
  type KeyboardEvent,
  type SubmitEvent,
} from "react";

import { api, unwrap } from "../api/client";
import { errorMessage } from "../api/errors";
import { MOBILE_QUERY, useMediaQuery } from "../lib/media";
import { Spinner } from "../ui/Spinner";
import { MAX_ATTACHMENTS, MAX_QUESTION, type Attachment } from "./api";
import styles from "./Chat.module.css";

const ACCEPT = ".docx,.doc,.xlsx,.pptx,.pdf,.txt,.md";
const MAX_FILE_BYTES = 10 * 1024 * 1024;

type Chip =
  | { key: string; name: string; state: "uploading" }
  | { key: string; name: string; state: "ready"; attachment: Attachment }
  | { key: string; name: string; state: "error"; error: string };

let chipCounter = 0;

function uploadAttachment(file: File): Promise<Attachment> {
  return unwrap(
    api.POST("/api/v1/attachments", {
      body: { file: file as unknown as string },
      bodySerializer: () => {
        const form = new FormData();
        form.append("file", file);
        return form;
      },
    }),
  );
}

export interface Draft {
  question: string;
  attachments: Attachment[];
}

/**
 * Поле вопроса: до 4 000 символов, файлы к вопросу (ТЗ §6), «Остановить»,
 * пока печатается ответ. onSend — true, если вопрос ушёл; иначе текст и
 * файлы остаются в поле.
 */
export function Composer({
  busy,
  stopping = false,
  onSend,
  onStop,
  autoFocus = true,
}: {
  busy: boolean;
  stopping?: boolean;
  onSend: (draft: Draft) => Promise<boolean>;
  onStop?: () => void;
  autoFocus?: boolean;
}) {
  const [value, setValue] = useState("");
  const [chips, setChips] = useState<Chip[]>([]);
  const [sending, setSending] = useState(false);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLTextAreaElement>(null);
  const files = useRef<HTMLInputElement>(null);
  const mobile = useMediaQuery(MOBILE_QUERY);
  const question = value.trim();
  const over = value.length > MAX_QUESTION;
  const uploading = chips.some((chip) => chip.state === "uploading");
  const disabled = busy || sending || !question || over || uploading;

  // Поле растёт вместе с текстом до max-height из стилей.
  useLayoutEffect(() => {
    const node = input.current;
    if (!node) return;
    node.style.height = "auto";
    node.style.height = `${node.scrollHeight + 2}px`;
  }, [value]);

  function addFiles(list: FileList | File[]) {
    const room = MAX_ATTACHMENTS - chips.filter((chip) => chip.state !== "error").length;
    for (const [index, file] of Array.from(list).entries()) {
      chipCounter += 1;
      const key = `f${chipCounter}`;
      if (index >= room) {
        setChips((prev) => [
          ...prev,
          {
            key,
            name: file.name,
            state: "error",
            error: `К вопросу — не больше ${MAX_ATTACHMENTS} файлов`,
          },
        ]);
        continue;
      }
      if (file.size > MAX_FILE_BYTES) {
        setChips((prev) => [
          ...prev,
          { key, name: file.name, state: "error", error: "Файл больше 10 МБ" },
        ]);
        continue;
      }
      setChips((prev) => [...prev, { key, name: file.name, state: "uploading" }]);
      uploadAttachment(file).then(
        (attachment) =>
          setChips((prev) =>
            prev.map((chip) =>
              chip.key === key ? { key, name: file.name, state: "ready", attachment } : chip,
            ),
          ),
        (err: unknown) =>
          setChips((prev) =>
            prev.map((chip) =>
              chip.key === key
                ? { key, name: file.name, state: "error", error: errorMessage(err) }
                : chip,
            ),
          ),
      );
    }
  }

  function choose(event: ChangeEvent<HTMLInputElement>) {
    if (event.target.files) addFiles(event.target.files);
    // Тот же файл ещё раз — тоже событие change.
    event.target.value = "";
  }

  function remove(chip: Chip) {
    setChips((prev) => prev.filter((item) => item.key !== chip.key));
    if (chip.state === "ready") {
      // Неотправленный файл сервер и сам удалит через сутки.
      void api
        .DELETE("/api/v1/attachments/{attachment_id}", {
          params: { path: { attachment_id: chip.attachment.id } },
        })
        .catch(() => undefined);
    }
  }

  async function submit(event?: SubmitEvent) {
    event?.preventDefault();
    if (disabled) return;
    const attachments = chips.flatMap((chip) => (chip.state === "ready" ? [chip.attachment] : []));
    setSending(true);
    try {
      if (await onSend({ question, attachments })) {
        setValue("");
        setChips([]);
      }
    } finally {
      setSending(false);
      input.current?.focus();
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void submit();
    }
  }

  function onDrop(event: DragEvent<HTMLFormElement>) {
    if (!event.dataTransfer.files.length) return;
    event.preventDefault();
    setDragging(false);
    addFiles(event.dataTransfer.files);
  }

  return (
    <div className={styles.composer}>
      <form
        className={`${styles.form} ${dragging ? styles.dragging : ""}`}
        onSubmit={(e) => void submit(e)}
        onDragOver={(e) => {
          if (e.dataTransfer.types.includes("Files")) {
            e.preventDefault();
            setDragging(true);
          }
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        {chips.length > 0 ? (
          <ul className={styles.chips} aria-label="Файлы к вопросу">
            {chips.map((chip) => (
              <li
                key={chip.key}
                className={`${styles.chip} ${chip.state === "error" ? styles.chipError : ""}`}
              >
                {chip.state === "uploading" ? (
                  <Spinner size={14} />
                ) : (
                  <FileText size={16} aria-hidden />
                )}
                <span className={styles.chipText}>
                  <span className={styles.attachedName}>{chip.name}</span>
                  {chip.state === "error" ? (
                    <span className={styles.chipNote}>{chip.error}</span>
                  ) : chip.state === "uploading" ? (
                    <span className={styles.chipNote}>Читаю файл…</span>
                  ) : null}
                </span>
                <button
                  type="button"
                  className={styles.chipRemove}
                  aria-label={`Убрать файл ${chip.name}`}
                  onClick={() => remove(chip)}
                >
                  <X size={14} aria-hidden />
                </button>
              </li>
            ))}
          </ul>
        ) : null}
        <label className="visually-hidden" htmlFor="question">
          Ваш вопрос
        </label>
        <textarea
          ref={input}
          id="question"
          className={styles.input}
          rows={1}
          autoComplete="off"
          placeholder={mobile ? "Вопрос по документам…" : "Задайте вопрос по документам компании…"}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
          aria-describedby="question-hint"
          autoFocus={autoFocus}
        />
        <input
          ref={files}
          type="file"
          accept={ACCEPT}
          multiple
          className="visually-hidden"
          tabIndex={-1}
          aria-hidden
          onChange={choose}
        />
        <button
          type="button"
          className={styles.attach}
          aria-label="Приложить файл"
          title="Приложить файл: он останется в этом диалоге и не попадёт в документы компании"
          onClick={() => files.current?.click()}
        >
          <Paperclip size={18} aria-hidden />
        </button>
        {busy && onStop ? (
          <button
            className={styles.send}
            type="button"
            onClick={onStop}
            disabled={stopping}
            aria-label="Остановить ответ"
          >
            <Square size={14} fill="currentColor" aria-hidden />
          </button>
        ) : (
          <button
            className={styles.send}
            type="submit"
            disabled={disabled}
            aria-label="Отправить вопрос"
          >
            <ArrowUp size={18} strokeWidth={2} aria-hidden />
          </button>
        )}
      </form>
      <div className={styles.composerFoot} id="question-hint">
        <span className={styles.privacy}>
          <ShieldCheck size={14} aria-hidden />
          Администратор видит вопросы только обезличенно
        </span>
        {value.length > MAX_QUESTION * 0.8 ? (
          <span className={over ? styles.over : undefined}>
            {value.length.toLocaleString("ru-RU")} / {MAX_QUESTION.toLocaleString("ru-RU")}
          </span>
        ) : null}
      </div>
    </div>
  );
}
