import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, Lightbulb, Pencil, Plus, Trash2 } from "lucide-react";
import { useEffect, useRef, useState, type SubmitEvent } from "react";

import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { SUGGESTIONS_KEY } from "../chat/keys";
import { useDocumentTitle } from "../lib/title";
import { Button } from "../ui/Button";
import { TextField } from "../ui/Field";
import { IconButton } from "../ui/IconButton";
import { Modal } from "../ui/Modal";
import { Notice } from "../ui/Notice";
import { EmptyState, Page, PageHeader } from "../ui/Page";
import pageStyles from "../ui/Page.module.css";
import { PageSpinner } from "../ui/Spinner";
import { Table } from "../ui/Table";
import tableStyles from "../ui/Table.module.css";
import { useToast } from "../ui/useToast";
import styles from "./Admin.module.css";
import { ConfirmDialog } from "./common";

type Suggestion = Schemas["SuggestionResponse"];
type Suggestions = Schemas["SuggestionsResponse"];

/** Как в suggestion_service.py бэкенда. */
const MAX_SUGGESTIONS = 12;
const MAX_TEXT = 200;

/**
 * Подсказки вопросов на пустом экране чата (ТЗ §6): свои задаёт
 * администратор, частые вопросы компании kronto собирает сам — только
 * обезличенно и только заданные несколькими разными людьми.
 */
export function SuggestionsPage() {
  useDocumentTitle("Подсказки");
  const queryClient = useQueryClient();
  const toast = useToast();
  const suggestions = useQuery({
    queryKey: SUGGESTIONS_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/suggestions")),
  });
  const [editing, setEditing] = useState<Suggestion | "new" | null>(null);
  const [deleting, setDeleting] = useState<Suggestion | null>(null);
  const company = suggestions.data?.company ?? [];
  const full = company.length >= MAX_SUGGESTIONS;

  // Порядок — с сервера; при отказе (список успели поменять) — свежий список.
  const reorder = useMutation({
    mutationFn: (ids: string[]) => unwrap(api.PUT("/api/v1/suggestions/order", { body: { ids } })),
    onSuccess: (list) =>
      queryClient.setQueryData<Suggestions>(SUGGESTIONS_KEY, (old) =>
        old ? { ...old, company: list } : old,
      ),
    onError: (error) => {
      toast.show(errorMessage(error), { tone: "error" });
      void queryClient.invalidateQueries({ queryKey: SUGGESTIONS_KEY });
    },
  });

  // Перемещённая строка переставляется в DOM, а кнопки на время запроса
  // выключены — фокус теряется. Возвращаем его на ту же кнопку (у края
  // списка — на соседнюю).
  const refocus = useRef<{ id: string; by: -1 | 1 } | null>(null);
  useEffect(() => {
    const moved = refocus.current;
    if (!moved || reorder.isPending) return;
    refocus.current = null;
    const buttons = Array.from(
      document.querySelectorAll<HTMLButtonElement>(`[data-move="${moved.id}"]:not(:disabled)`),
    );
    (buttons.find((b) => b.dataset.by === String(moved.by)) ?? buttons[0])?.focus();
  }, [suggestions.data, reorder.isPending]);

  function move(item: Suggestion, index: number, by: -1 | 1) {
    const other = company[index + by];
    if (!other) return;
    const ids = company.map((s) => s.id);
    ids[index] = other.id;
    ids[index + by] = item.id;
    refocus.current = { id: item.id, by };
    reorder.mutate(ids);
  }

  return (
    <Page>
      <PageHeader
        label="управление"
        title="Подсказки"
        description="Сотрудники видят подсказки на пустом экране чата — выше частых вопросов. Частые вопросы kronto собирает сам: только обезличенно и только те, что задали хотя бы три разных человека."
        actions={
          <Button
            size="sm"
            disabled={full}
            aria-describedby={full ? "suggestions-count" : undefined}
            onClick={() => setEditing("new")}
          >
            <Plus size={16} aria-hidden /> Добавить подсказку
          </Button>
        }
      />
      {suggestions.isPending ? (
        <PageSpinner />
      ) : suggestions.isError ? (
        <Notice kind="error">{errorMessage(suggestions.error)}</Notice>
      ) : (
        <>
          {company.length === 0 ? (
            <EmptyState icon={<Lightbulb size={32} aria-hidden />} title="Подсказок пока нет">
              <p>Добавьте вопросы, с которых сотрудникам удобно начать разговор с kronto.</p>
            </EmptyState>
          ) : (
            <>
              <p className="muted" id="suggestions-count" style={{ marginBottom: 12 }}>
                {full
                  ? `Добавлено ${MAX_SUGGESTIONS} из ${MAX_SUGGESTIONS} — больше не поместится. Чтобы добавить новую, удалите одну из подсказок.`
                  : `Подсказок: ${company.length} из ${MAX_SUGGESTIONS}.`}
              </p>
              <Table label="Подсказки">
                <thead>
                  <tr>
                    <th>Подсказка</th>
                    <th className={tableStyles.actions}>
                      <span className="visually-hidden">Действия</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {company.map((item, index) => (
                    <tr key={item.id}>
                      <td style={{ overflowWrap: "anywhere" }}>{item.text}</td>
                      <td className={tableStyles.actions}>
                        <span className={styles.rowActions}>
                          <IconButton
                            size="sm"
                            label="Переместить выше"
                            data-move={item.id}
                            data-by={-1}
                            disabled={index === 0 || reorder.isPending}
                            onClick={() => move(item, index, -1)}
                          >
                            <ArrowUp size={16} aria-hidden />
                          </IconButton>
                          <IconButton
                            size="sm"
                            label="Переместить ниже"
                            data-move={item.id}
                            data-by={1}
                            disabled={index === company.length - 1 || reorder.isPending}
                            onClick={() => move(item, index, 1)}
                          >
                            <ArrowDown size={16} aria-hidden />
                          </IconButton>
                          <IconButton size="sm" label="Изменить" onClick={() => setEditing(item)}>
                            <Pencil size={16} aria-hidden />
                          </IconButton>
                          <IconButton size="sm" label="Удалить" onClick={() => setDeleting(item)}>
                            <Trash2 size={16} aria-hidden />
                          </IconButton>
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </>
          )}
          <section className={pageStyles.section} aria-labelledby="frequent-title">
            <h2 className={pageStyles.sectionTitle} id="frequent-title">
              Частые вопросы сотрудников
            </h2>
            <p className="muted">
              Собираются сами из вопросов за последние три месяца — без имён и персональных данных.
              Изменить их нельзя: чтобы сформулировать вопрос иначе, добавьте его своей подсказкой.
            </p>
            {suggestions.data.frequent.length === 0 ? (
              <p className="muted" style={{ marginTop: 12 }}>
                Пока нет: вопрос появится здесь, когда его зададут хотя бы трое разных коллег.
              </p>
            ) : (
              <ul className={styles.samples}>
                {suggestions.data.frequent.map((question) => (
                  <li key={question}>«{question}»</li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
      {editing ? (
        <SuggestionDialog
          suggestion={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
        />
      ) : null}
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => !open && setDeleting(null)}
        title="Удалить подсказку?"
        description={
          deleting
            ? `«${deleting.text}» пропадёт с пустого экрана чата у всех сотрудников.`
            : undefined
        }
        confirmLabel="Удалить"
        onConfirm={async () => {
          if (!deleting) return;
          await unwrap(
            api.DELETE("/api/v1/suggestions/{suggestion_id}", {
              params: { path: { suggestion_id: deleting.id } },
            }),
          );
          await queryClient.invalidateQueries({ queryKey: SUGGESTIONS_KEY });
        }}
      />
    </Page>
  );
}

function SuggestionDialog({
  suggestion,
  onClose,
}: {
  suggestion: Suggestion | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [text, setText] = useState(suggestion?.text ?? "");
  const value = text.split(/\s+/).filter(Boolean).join(" ");
  // Частые вопросы, совпавшие с подсказкой, сервер не повторяет — после
  // правки обновляем список целиком.
  const save = useMutation({
    mutationFn: () =>
      suggestion
        ? unwrap(
            api.PATCH("/api/v1/suggestions/{suggestion_id}", {
              params: { path: { suggestion_id: suggestion.id } },
              body: { text: value },
            }),
          )
        : unwrap(api.POST("/api/v1/suggestions", { body: { text: value } })),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: SUGGESTIONS_KEY });
      onClose();
    },
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    if (value) save.mutate();
  }

  return (
    <Modal
      open
      onOpenChange={(open) => !open && onClose()}
      title={suggestion ? "Изменить подсказку" : "Новая подсказка"}
    >
      <form className={pageStyles.form} onSubmit={submit}>
        {save.isError ? <Notice kind="error">{errorMessage(save.error)}</Notice> : null}
        <TextField
          label="Вопрос"
          required
          maxLength={MAX_TEXT}
          value={text}
          onChange={(e) => setText(e.target.value)}
          hint="Коротко и так, как спросил бы сотрудник: «Как оформить отпуск?»"
          autoFocus
        />
        <div className={pageStyles.row} style={{ justifyContent: "flex-end" }}>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Отмена
          </Button>
          <Button type="submit" size="sm" busy={save.isPending} disabled={!value}>
            Сохранить
          </Button>
        </div>
      </form>
    </Modal>
  );
}
