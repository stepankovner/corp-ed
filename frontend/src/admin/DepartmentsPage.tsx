import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Network, Pencil, Plus, Trash2 } from "lucide-react";
import { useState, type SubmitEvent } from "react";
import { Link } from "react-router";

import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { plural } from "../lib/format";
import { useDocumentTitle } from "../lib/title";
import { DEPARTMENTS_KEY, PEOPLE_KEY } from "../people/keys";
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

type Department = Schemas["DepartmentResponse"];

/** Как в схеме бэкенда (DepartmentRequest). */
const MAX_NAME = 100;

/**
 * Отделы компании (ТЗ §7): заводит администратор, сотрудник выбирает свой
 * в профиле, коллег можно отобрать по отделу в справочнике. Дальше к
 * отделам привяжется доступ к папкам документов (§5).
 */
export function DepartmentsPage() {
  useDocumentTitle("Отделы");
  const queryClient = useQueryClient();
  const departments = useQuery({
    queryKey: DEPARTMENTS_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/departments")),
  });
  const [editing, setEditing] = useState<Department | "new" | null>(null);
  const [deleting, setDeleting] = useState<Department | null>(null);

  return (
    <Page>
      <PageHeader
        label="управление"
        title="Отделы"
        description="Сотрудники выбирают свой отдел в профиле, а вы можете поправить его в разделе «Сотрудники» или в справочнике коллег."
        actions={
          <Button size="sm" onClick={() => setEditing("new")}>
            <Plus size={16} aria-hidden /> Добавить отдел
          </Button>
        }
      />
      {departments.isPending ? (
        <PageSpinner />
      ) : departments.isError ? (
        <Notice kind="error">{errorMessage(departments.error)}</Notice>
      ) : departments.data.length === 0 ? (
        <EmptyState icon={<Network size={32} aria-hidden />} title="Отделов пока нет">
          <p>Добавьте отделы — сотрудники выберут свой, а коллег можно будет найти по отделу.</p>
        </EmptyState>
      ) : (
        <Table label="Отделы">
          <thead>
            <tr>
              <th>Отдел</th>
              <th>Сотрудников</th>
              <th className={tableStyles.actions}>
                <span className="visually-hidden">Действия</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {departments.data.map((item) => (
              <tr key={item.id}>
                <td>{item.name}</td>
                <td>{item.members}</td>
                <td className={tableStyles.actions}>
                  <span className={styles.rowActions}>
                    <IconButton size="sm" label="Переименовать" onClick={() => setEditing(item)}>
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
      )}
      <p className={`muted ${pageStyles.description}`} style={{ marginTop: "var(--s-5)" }}>
        Кто в каком отделе — в <Link to="/people">справочнике коллег</Link>.
      </p>
      {editing ? (
        <DepartmentDialog
          department={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
        />
      ) : null}
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => !open && setDeleting(null)}
        title="Удалить отдел?"
        description={
          deleting
            ? deleting.members > 0
              ? `«${deleting.name}» уберётся у ${deleting.members} ${plural(deleting.members, "сотрудника", "сотрудников", "сотрудников")}: они останутся в компании без отдела.`
              : `«${deleting.name}» пропадёт из списка отделов.`
            : undefined
        }
        confirmLabel="Удалить"
        onConfirm={async () => {
          if (!deleting) return;
          await unwrap(
            api.DELETE("/api/v1/departments/{department_id}", {
              params: { path: { department_id: deleting.id } },
            }),
          );
          await Promise.all([
            queryClient.invalidateQueries({ queryKey: DEPARTMENTS_KEY }),
            queryClient.invalidateQueries({ queryKey: PEOPLE_KEY }),
          ]);
        }}
      />
    </Page>
  );
}

function DepartmentDialog({
  department,
  onClose,
}: {
  department: Department | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(department?.name ?? "");
  const value = name.split(/\s+/).filter(Boolean).join(" ");
  const save = useMutation({
    mutationFn: () =>
      department
        ? unwrap(
            api.PATCH("/api/v1/departments/{department_id}", {
              params: { path: { department_id: department.id } },
              body: { name: value },
            }),
          )
        : unwrap(api.POST("/api/v1/departments", { body: { name: value } })),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: DEPARTMENTS_KEY }),
        queryClient.invalidateQueries({ queryKey: PEOPLE_KEY }),
      ]);
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
      title={department ? "Переименовать отдел" : "Новый отдел"}
    >
      <form className={pageStyles.form} onSubmit={submit}>
        {save.isError ? <Notice kind="error">{errorMessage(save.error)}</Notice> : null}
        <TextField
          label="Название"
          required
          maxLength={MAX_NAME}
          value={name}
          onChange={(e) => setName(e.target.value)}
          hint="Например: «Продажи», «Бухгалтерия», «Отдел кадров»."
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
