import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Pencil, UserPlus } from "lucide-react";
import { useMemo, useState, type SubmitEvent } from "react";

import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { useMe } from "../auth/context";
import { formatDateTime, formatRelative } from "../lib/format";
import { useDocumentTitle } from "../lib/title";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Checkbox, SelectField, TextField } from "../ui/Field";
import { IconButton } from "../ui/IconButton";
import { Modal } from "../ui/Modal";
import { Notice } from "../ui/Notice";
import { Page, PageHeader } from "../ui/Page";
import pageStyles from "../ui/Page.module.css";
import { PageSpinner } from "../ui/Spinner";
import { Table } from "../ui/Table";
import tableStyles from "../ui/Table.module.css";
import styles from "./Admin.module.css";
import { ConfirmDialog, SecretValue } from "./common";
import { InvitesSection } from "./InvitesSection";

type User = Schemas["UserResponse"];
type Role = Schemas["UserRole"];

const ROLE_LABEL: Record<Role, string> = { admin: "администратор", employee: "сотрудник" };

export function UsersPage() {
  useDocumentTitle("Сотрудники");
  const me = useMe();
  const queryClient = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: () => unwrap(api.GET("/api/v1/users")) });
  // Места — из того же ответа, что лимит вопросов: активных учёток не
  // больше мест (решение 28.09), заблокированные место не занимают.
  const usage = useQuery({ queryKey: ["usage"], queryFn: () => unwrap(api.GET("/api/v1/usage")) });
  const active = (users.data ?? []).filter((u) => u.is_active).length;
  const seats = usage.data?.seats;
  const full = seats !== undefined && active >= seats;
  const [search, setSearch] = useState("");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<User | null>(null);
  const [resetting, setResetting] = useState<User | null>(null);
  const [secret, setSecret] = useState<{ email: string; password: string } | null>(null);

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return (users.data ?? []).filter(
      (u) =>
        !needle ||
        u.email.toLowerCase().includes(needle) ||
        (u.full_name ?? "").toLowerCase().includes(needle),
    );
  }, [users.data, search]);

  const toggleActive = useMutation({
    mutationFn: (user: User) =>
      unwrap(
        api.PATCH("/api/v1/users/{user_id}", {
          params: { path: { user_id: user.id } },
          body: { is_active: !user.is_active },
        }),
      ),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["users"] }),
  });

  return (
    <Page>
      <PageHeader
        label="управление"
        title="Сотрудники"
        description="Каждый сотрудник входит по коду компании, почте и паролю. Добавьте сотрудника с временным паролем или отправьте ссылку-приглашение — по ней он сам заведёт учётку."
        actions={
          <Button size="sm" onClick={() => setCreating(true)}>
            <UserPlus size={16} aria-hidden /> Добавить сотрудника
          </Button>
        }
      />
      {toggleActive.isError ? (
        <Notice kind="error">{errorMessage(toggleActive.error)}</Notice>
      ) : null}
      {seats !== undefined && users.data ? (
        full ? (
          <Notice kind="warn" title={`Все места заняты: ${active} из ${seats}`}>
            Новых сотрудников добавить нельзя — ни вручную, ни по ссылке. Заблокируйте тех, кто
            больше не работает, или напишите нам, чтобы добавить места.
          </Notice>
        ) : (
          <p className="muted" style={{ marginBottom: 12 }}>
            Активных сотрудников: {active} из {seats} мест. Заблокированные место не занимают.
          </p>
        )
      ) : null}
      {users.isPending ? (
        <PageSpinner />
      ) : users.isError ? (
        <Notice kind="error">{errorMessage(users.error)}</Notice>
      ) : (
        <>
          <div className={styles.toolbar}>
            <span className={styles.searchWrap}>
              <input
                className={styles.search}
                type="search"
                placeholder="Поиск по имени или почте"
                aria-label="Поиск по имени или почте"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </span>
          </div>
          <Table label="Сотрудники">
            <thead>
              <tr>
                <th>Сотрудник</th>
                <th>Роль</th>
                <th>Состояние</th>
                <th>Последний вход</th>
                <th className={tableStyles.actions}>
                  <span className="visually-hidden">Действия</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {visible.map((user) => {
                const self = user.id === me.id;
                return (
                  <tr key={user.id}>
                    <td>
                      <span style={{ fontWeight: 500 }}>{user.full_name || user.email}</span>
                      {user.full_name ? (
                        <span className={tableStyles.sub}>{user.email}</span>
                      ) : null}
                    </td>
                    <td>
                      <Badge tone={user.role === "admin" ? "accent" : "muted"}>
                        {ROLE_LABEL[user.role]}
                      </Badge>
                    </td>
                    <td>
                      {!user.is_active ? (
                        <Badge tone="error">заблокирован</Badge>
                      ) : user.must_change_password ? (
                        <Badge tone="warn">временный пароль</Badge>
                      ) : (
                        <Badge tone="ok">активен</Badge>
                      )}
                    </td>
                    <td className={tableStyles.nowrap} title={formatDateTime(user.last_login_at)}>
                      {user.last_login_at ? formatRelative(user.last_login_at) : "не входил"}
                    </td>
                    <td className={tableStyles.actions}>
                      <span className={styles.rowActions}>
                        <IconButton size="sm" label="Изменить" onClick={() => setEditing(user)}>
                          <Pencil size={16} aria-hidden />
                        </IconButton>
                        <IconButton
                          size="sm"
                          label="Выдать временный пароль"
                          disabled={self}
                          onClick={() => setResetting(user)}
                        >
                          <KeyRound size={16} aria-hidden />
                        </IconButton>
                        <Button
                          variant={user.is_active ? "danger" : "ghost"}
                          size="xs"
                          disabled={self || toggleActive.isPending}
                          onClick={() => toggleActive.mutate(user)}
                        >
                          {user.is_active ? "Заблокировать" : "Разблокировать"}
                        </Button>
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </>
      )}
      <InvitesSection />

      {creating ? (
        <CreateUserDialog
          onClose={() => setCreating(false)}
          onCreated={(email, password) => {
            setCreating(false);
            if (password) setSecret({ email, password });
          }}
        />
      ) : null}
      {editing ? (
        <EditUserDialog
          user={editing}
          self={editing.id === me.id}
          onClose={() => setEditing(null)}
        />
      ) : null}
      <ConfirmDialog
        open={resetting !== null}
        onOpenChange={(open) => !open && setResetting(null)}
        title="Выдать временный пароль?"
        description={
          resetting
            ? `Текущий пароль ${resetting.email} перестанет работать, все его сеансы завершатся.`
            : undefined
        }
        confirmLabel="Выдать"
        danger={false}
        onConfirm={async () => {
          if (!resetting) return;
          const result = await unwrap(
            api.POST("/api/v1/users/{user_id}/reset-password", {
              params: { path: { user_id: resetting.id } },
            }),
          );
          setSecret({ email: resetting.email, password: result.temporary_password });
          await queryClient.invalidateQueries({ queryKey: ["users"] });
        }}
      />
      <Modal
        open={secret !== null}
        onOpenChange={(open) => !open && setSecret(null)}
        title="Временный пароль"
        description={
          secret
            ? `Передайте его ${secret.email} лично или в защищённом канале. Больше он не покажется.`
            : undefined
        }
        footer={
          <Button size="sm" onClick={() => setSecret(null)}>
            Готово
          </Button>
        }
      >
        {secret ? <SecretValue value={secret.password} /> : null}
      </Modal>
    </Page>
  );
}

function CreateUserDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (email: string, password: string | null) => void;
}) {
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("");
  const [fullName, setFullName] = useState("");
  const [admin, setAdmin] = useState(false);
  const create = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/users", {
          body: {
            email: email.trim(),
            full_name: fullName.trim() || null,
            role: admin ? "admin" : "employee",
          },
        }),
      ),
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: ["users"] });
      onCreated(result.user.email, result.temporary_password ?? null);
    },
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    create.mutate();
  }

  return (
    <Modal open onOpenChange={(open) => !open && onClose()} title="Новый сотрудник">
      <form className={pageStyles.form} onSubmit={submit}>
        {create.isError ? <Notice kind="error">{errorMessage(create.error)}</Notice> : null}
        <TextField
          label="Рабочая почта"
          type="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoFocus
        />
        <TextField
          label="Имя и фамилия"
          optional
          value={fullName}
          onChange={(e) => setFullName(e.target.value)}
        />
        <Checkbox
          label="Администратор — управляет документами, подключениями и сотрудниками"
          checked={admin}
          onChange={(e) => setAdmin(e.target.checked)}
        />
        <div className={pageStyles.row} style={{ justifyContent: "flex-end" }}>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Отмена
          </Button>
          <Button type="submit" size="sm" busy={create.isPending} disabled={!email.trim()}>
            Добавить
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function EditUserDialog({
  user,
  self,
  onClose,
}: {
  user: User;
  self: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [fullName, setFullName] = useState(user.full_name ?? "");
  const [role, setRole] = useState<Role>(user.role);
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        api.PATCH("/api/v1/users/{user_id}", {
          params: { path: { user_id: user.id } },
          // Пустая строка стирает имя: null сервер понял бы как «не менять».
          body: { full_name: fullName.trim(), ...(self ? {} : { role }) },
        }),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["users"] });
      onClose();
    },
  });
  return (
    <Modal open onOpenChange={(open) => !open && onClose()} title={user.email}>
      <form
        className={pageStyles.form}
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        {save.isError ? <Notice kind="error">{errorMessage(save.error)}</Notice> : null}
        <TextField
          label="Имя и фамилия"
          optional
          value={fullName}
          onChange={(e) => setFullName(e.target.value)}
        />
        <SelectField
          label="Роль"
          value={role}
          onChange={(e) => setRole(e.target.value as Role)}
          disabled={self}
          hint={self ? "Свою роль изменить нельзя." : undefined}
        >
          <option value="employee">Сотрудник</option>
          <option value="admin">Администратор</option>
        </SelectField>
        <div className={pageStyles.row} style={{ justifyContent: "flex-end" }}>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Отмена
          </Button>
          <Button type="submit" size="sm" busy={save.isPending}>
            Сохранить
          </Button>
        </div>
      </form>
    </Modal>
  );
}
