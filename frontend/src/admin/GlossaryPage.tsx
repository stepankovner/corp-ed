import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookA, Pencil, Plus, Trash2 } from "lucide-react";
import { useState, type SubmitEvent } from "react";

import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
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
import styles from "./Admin.module.css";
import { ConfirmDialog } from "./common";

type Term = Schemas["GlossaryTermResponse"];

export function GlossaryPage() {
  useDocumentTitle("Глоссарий");
  const queryClient = useQueryClient();
  const terms = useQuery({
    queryKey: ["glossary"],
    queryFn: () => unwrap(api.GET("/api/v1/glossary")),
  });
  const [editing, setEditing] = useState<Term | "new" | null>(null);
  const [deleting, setDeleting] = useState<Term | null>(null);

  return (
    <Page>
      <PageHeader
        label="управление"
        title="Глоссарий"
        description="Внутренние сокращения и жаргон компании. Kronto разворачивает их в вопросе перед поиском: «СЗ на ДМС» найдёт «служебную записку» про «добровольное медицинское страхование»."
        actions={
          <Button size="sm" onClick={() => setEditing("new")}>
            <Plus size={16} aria-hidden /> Добавить термин
          </Button>
        }
      />
      {terms.isPending ? (
        <PageSpinner />
      ) : terms.isError ? (
        <Notice kind="error">{errorMessage(terms.error)}</Notice>
      ) : terms.data.length === 0 ? (
        <EmptyState icon={<BookA size={32} aria-hidden />} title="Терминов пока нет">
          <p>Добавьте сокращения, которые сотрудники пишут в вопросах.</p>
        </EmptyState>
      ) : (
        <Table label="Глоссарий">
          <thead>
            <tr>
              <th>Термин</th>
              <th>Расшифровка</th>
              <th className={tableStyles.actions}>
                <span className="visually-hidden">Действия</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {terms.data.map((term) => (
              <tr key={term.id}>
                <td className="mono" style={{ fontSize: 14 }}>
                  {term.term}
                </td>
                <td>{term.expansion}</td>
                <td className={tableStyles.actions}>
                  <span className={styles.rowActions}>
                    <IconButton size="sm" label="Изменить" onClick={() => setEditing(term)}>
                      <Pencil size={16} aria-hidden />
                    </IconButton>
                    <IconButton size="sm" label="Удалить" onClick={() => setDeleting(term)}>
                      <Trash2 size={16} aria-hidden />
                    </IconButton>
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      {editing ? (
        <TermDialog term={editing === "new" ? null : editing} onClose={() => setEditing(null)} />
      ) : null}
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => !open && setDeleting(null)}
        title="Удалить термин?"
        description={
          deleting ? `«${deleting.term}» перестанет разворачиваться в вопросах.` : undefined
        }
        confirmLabel="Удалить"
        onConfirm={async () => {
          if (!deleting) return;
          await unwrap(
            api.DELETE("/api/v1/glossary/{term_id}", {
              params: { path: { term_id: deleting.id } },
            }),
          );
          await queryClient.invalidateQueries({ queryKey: ["glossary"] });
        }}
      />
    </Page>
  );
}

function TermDialog({ term, onClose }: { term: Term | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [value, setValue] = useState(term?.term ?? "");
  const [expansion, setExpansion] = useState(term?.expansion ?? "");
  const save = useMutation({
    mutationFn: () => {
      const body = { term: value.trim(), expansion: expansion.trim() };
      return term
        ? unwrap(
            api.PATCH("/api/v1/glossary/{term_id}", {
              params: { path: { term_id: term.id } },
              body,
            }),
          )
        : unwrap(api.POST("/api/v1/glossary", { body }));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["glossary"] });
      onClose();
    },
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    save.mutate();
  }

  const valid = value.trim().length >= 2 && expansion.trim().length >= 2;
  return (
    <Modal
      open
      onOpenChange={(open) => !open && onClose()}
      title={term ? "Изменить термин" : "Новый термин"}
    >
      <form className={pageStyles.form} onSubmit={submit}>
        {save.isError ? <Notice kind="error">{errorMessage(save.error)}</Notice> : null}
        <TextField
          label="Термин"
          required
          minLength={2}
          maxLength={64}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          hint="Как пишут сотрудники: СЗ, ДМС, ПТО."
          autoFocus
        />
        <TextField
          label="Расшифровка"
          required
          minLength={2}
          maxLength={256}
          value={expansion}
          onChange={(e) => setExpansion(e.target.value)}
        />
        <div className={pageStyles.row} style={{ justifyContent: "flex-end" }}>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Отмена
          </Button>
          <Button type="submit" size="sm" busy={save.isPending} disabled={!valid}>
            Сохранить
          </Button>
        </div>
      </form>
    </Modal>
  );
}
