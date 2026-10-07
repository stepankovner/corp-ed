import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Ban,
  BriefcaseBusiness,
  Ellipsis,
  LockOpen,
  ShieldCheck,
  UserMinus,
  UserRound,
} from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { useCompany } from "../auth/context";
import { formatDateTime, formatRelative } from "../lib/format";
import { personInitials } from "../lib/initials";
import { useDocumentTitle } from "../lib/title";
import { DEPARTMENTS_KEY } from "../people/keys";
import { WorkEditDialog, type WorkTarget } from "../people/WorkEditDialog";
import { Avatar } from "../ui/Avatar";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "../ui/DropdownMenu";
import { IconButton } from "../ui/IconButton";
import { Notice } from "../ui/Notice";
import { Page, PageHeader } from "../ui/Page";
import pageStyles from "../ui/Page.module.css";
import { SkeletonList } from "../ui/Skeleton";
import { Table } from "../ui/Table";
import tableStyles from "../ui/Table.module.css";
import { useToast } from "../ui/useToast";
import styles from "./Admin.module.css";
import { ConfirmDialog } from "./common";
import { InviteButton, InvitesSection } from "./InvitesSection";

type Member = Schemas["UserResponse"];
type Role = Schemas["UserRole"];
type Change = Schemas["UserUpdateRequest"];

const ROLE_LABEL: Record<Role, string> = { admin: "администратор", employee: "сотрудник" };

/** Имя в списке: без имени — почта, без учётки (человек её удалил) — пометка. */
function memberName(member: Member): string {
  return member.full_name || member.email || "Учётка удалена";
}

function changeMessage(member: Member, change: Change): string {
  const name = memberName(member);
  if (change.role) return `${name} — теперь ${ROLE_LABEL[change.role]}`;
  return change.blocked ? `Доступ закрыт: ${name}` : `Доступ открыт: ${name}`;
}

/**
 * Люди компании (ТЗ §2, §7). Учёток админ не заводит и паролей не выдаёт
 * (решение 03.10): люди вступают по приглашению своей учёткой kronto, а
 * здесь — заявки на вступление, роли, блокировка и удаление из компании.
 */
export function UsersPage() {
  useDocumentTitle("Сотрудники");
  const company = useCompany();
  const toast = useToast();
  const queryClient = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: () => unwrap(api.GET("/api/v1/users")) });
  // Места — из того же ответа, что лимит вопросов: место занимают только
  // работающие, заблокированные и ждущие одобрения — нет (решение 28.09).
  const usage = useQuery({ queryKey: ["usage"], queryFn: () => unwrap(api.GET("/api/v1/usage")) });
  // Названия отделов для подписи под именем (в списке — только id).
  const departments = useQuery({
    queryKey: DEPARTMENTS_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/departments")),
  });
  const [search, setSearch] = useState("");
  const [removing, setRemoving] = useState<Member | null>(null);
  const [editingWork, setEditingWork] = useState<WorkTarget | null>(null);
  const departmentName = useMemo(() => {
    const names = new Map((departments.data ?? []).map((item) => [item.id, item.name]));
    return (member: Member) => (member.department_id ? names.get(member.department_id) : undefined);
  }, [departments.data]);

  const list = useMemo(() => users.data ?? [], [users.data]);
  const pending = list.filter((m) => m.status === "pending");
  // Отдел, выбранный самим сотрудником, ждёт подтверждения (ТЗ §7): до
  // него закрытые папки отдела человеку не открыты.
  const unconfirmed = list.filter(
    (m) => m.status === "active" && m.department_id && !m.department_confirmed,
  );
  const active = list.filter((m) => m.status === "active").length;
  const seats = usage.data?.seats;
  const full = seats !== undefined && active >= seats;

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return list.filter(
      (m) =>
        m.status !== "pending" &&
        (!needle ||
          [m.email, m.full_name, m.position, departmentName(m)].some((value) =>
            (value ?? "").toLowerCase().includes(needle),
          )),
    );
  }, [list, search, departmentName]);

  // Меню закрывается сразу, поэтому итог (и отказ сервера: последний
  // администратор, нет мест) — во всплывающем сообщении.
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["users"] });
  const fail = (error: Error) => toast.show(errorMessage(error), { tone: "error" });
  const update = useMutation({
    mutationFn: ({ member, change }: { member: Member; change: Change }) =>
      unwrap(
        api.PATCH("/api/v1/users/{user_id}", {
          params: { path: { user_id: member.id } },
          body: change,
        }),
      ),
    onSuccess: (_, { member, change }) => toast.show(changeMessage(member, change)),
    onError: fail,
    onSettled: refresh,
  });
  const approve = useMutation({
    mutationFn: (member: Member) =>
      unwrap(
        api.POST("/api/v1/users/{user_id}/approve", {
          params: { path: { user_id: member.id } },
        }),
      ),
    onSuccess: (_, member) => toast.show(`Заявка одобрена: ${memberName(member)}`),
    onError: fail,
    onSettled: refresh,
  });
  const reject = useMutation({
    mutationFn: (member: Member) =>
      unwrap(
        api.POST("/api/v1/users/{user_id}/reject", {
          params: { path: { user_id: member.id } },
        }),
      ),
    onSuccess: (_, member) => toast.show(`Заявка отклонена: ${memberName(member)}`),
    onError: fail,
    onSettled: refresh,
  });
  const deciding = (member: Member) =>
    (approve.isPending && approve.variables.id === member.id) ||
    (reject.isPending && reject.variables.id === member.id);
  // Подтверждение отдела меняет и счётчики отделов («ждут подтверждения»).
  const refreshDepartments = () =>
    Promise.all([refresh(), queryClient.invalidateQueries({ queryKey: DEPARTMENTS_KEY })]);
  const confirmDepartment = useMutation({
    mutationFn: (member: Member) =>
      unwrap(
        api.POST("/api/v1/users/{user_id}/department/confirm", {
          params: { path: { user_id: member.id } },
          // Отдел, который видит администратор: если сотрудник успел его
          // сменить, сервер ответит 409, а не решит за другой отдел.
          body: { department_id: member.department_id ?? "" },
        }),
      ),
    onSuccess: (_, member) =>
      toast.show(`Отдел подтверждён: ${memberName(member)} — ${departmentName(member) ?? ""}`),
    onError: fail,
    onSettled: refreshDepartments,
  });
  const rejectDepartment = useMutation({
    mutationFn: (member: Member) =>
      unwrap(
        api.POST("/api/v1/users/{user_id}/department/reject", {
          params: { path: { user_id: member.id } },
          // Отдел, который видит администратор: если сотрудник успел его
          // сменить, сервер ответит 409, а не решит за другой отдел.
          body: { department_id: member.department_id ?? "" },
        }),
      ),
    onSuccess: (_, member) => toast.show(`Отдел снят: ${memberName(member)}`),
    onError: fail,
    onSettled: refreshDepartments,
  });
  const decidingDepartment = (member: Member) =>
    (confirmDepartment.isPending && confirmDepartment.variables.id === member.id) ||
    (rejectDepartment.isPending && rejectDepartment.variables.id === member.id);

  return (
    <Page>
      <PageHeader
        label="управление"
        title="Сотрудники"
        description="Сотрудники вступают сами — по ссылке или коду приглашения, своей учёткой kronto; пароль восстанавливают по почте. Здесь — заявки на вступление, подтверждение отделов, роли и доступ к компании."
        actions={<InviteButton />}
      />
      {seats !== undefined && users.data ? (
        full ? (
          <Notice kind="warn" title={`Все места заняты: ${active} из ${seats}`}>
            Новые сотрудники не смогут вступить, а заявки — получить одобрение. Заблокируйте или
            уберите из компании тех, кто больше не работает, или напишите нам, чтобы добавить места.
          </Notice>
        ) : (
          <p className="muted" style={{ marginBottom: 12 }}>
            Занято мест: {active} из {seats}. Заблокированные и ждущие одобрения место не занимают.
          </p>
        )
      ) : null}
      {users.isPending ? (
        <SkeletonList label="Загрузка сотрудников" />
      ) : users.isError ? (
        <Notice kind="error">{errorMessage(users.error)}</Notice>
      ) : (
        <>
          {pending.length > 0 ? (
            <section aria-labelledby="pending-title" style={{ marginBottom: "var(--s-6)" }}>
              <h2 className={pageStyles.sectionTitle} id="pending-title">
                Ждут одобрения
              </h2>
              <p className="muted" style={{ marginBottom: 12 }}>
                Вступили по приглашению с одобрением. Одобренный сразу получит доступ и займёт
                рабочее место.
              </p>
              <ul className={styles.cards} aria-label="Заявки на вступление">
                {pending.map((member) => (
                  <li key={member.id} className={styles.card}>
                    <div className={styles.cardHead}>
                      <Person member={member}>
                        <span className={tableStyles.sub} title={formatDateTime(member.created_at)}>
                          заявка {formatRelative(member.created_at)}
                        </span>
                      </Person>
                      <span className={pageStyles.row} style={{ gap: 8 }}>
                        <Button
                          variant="ghost"
                          size="xs"
                          disabled={deciding(member)}
                          onClick={() => reject.mutate(member)}
                        >
                          Отклонить
                        </Button>
                        <Button
                          size="xs"
                          disabled={deciding(member)}
                          onClick={() => approve.mutate(member)}
                        >
                          Одобрить
                        </Button>
                      </span>
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
          {unconfirmed.length > 0 ? (
            <section aria-labelledby="departments-title" style={{ marginBottom: "var(--s-6)" }}>
              <h2 className={pageStyles.sectionTitle} id="departments-title">
                Ждут подтверждения отдела
              </h2>
              <p className="muted" style={{ marginBottom: 12 }}>
                Сотрудник сам указал отдел. Закрытые папки отдела откроются ему только после
                подтверждения; «Отклонить» — отдел снимется.
              </p>
              <ul className={styles.cards} aria-label="Отделы, ждущие подтверждения">
                {unconfirmed.map((member) => (
                  <li key={member.id} className={styles.card}>
                    <div className={styles.cardHead}>
                      <Person member={member}>
                        <span className={tableStyles.sub}>
                          отдел «{departmentName(member) ?? "…"}»
                        </span>
                      </Person>
                      <span className={pageStyles.row} style={{ gap: 8 }}>
                        <Button
                          variant="ghost"
                          size="xs"
                          disabled={decidingDepartment(member)}
                          onClick={() => rejectDepartment.mutate(member)}
                        >
                          Отклонить
                        </Button>
                        <Button
                          size="xs"
                          disabled={decidingDepartment(member)}
                          onClick={() => confirmDepartment.mutate(member)}
                        >
                          Подтвердить
                        </Button>
                      </span>
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
          <div className={styles.toolbar}>
            <span className={styles.searchWrap}>
              <input
                className={styles.search}
                type="search"
                placeholder="Имя, почта, должность, отдел"
                aria-label="Поиск по имени, почте, должности, отделу"
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
              {visible.map((member) => {
                // Себе роль не меняют, себя не блокируют и не убирают — сервер
                // всё равно ответит 409. Сравниваем с членством, а не с учёткой.
                const self = member.id === company.member_id;
                return (
                  <tr key={member.id}>
                    <td>
                      <Person member={member} self={self}>
                        <Work
                          position={member.position}
                          department={departmentName(member)}
                          unconfirmed={
                            Boolean(member.department_id) && !member.department_confirmed
                          }
                        />
                      </Person>
                    </td>
                    <td>
                      <Badge tone={member.role === "admin" ? "accent" : "muted"}>
                        {ROLE_LABEL[member.role]}
                      </Badge>
                    </td>
                    <td>
                      {member.status === "blocked" ? (
                        <Badge tone="error">заблокирован</Badge>
                      ) : (
                        <Badge tone="ok">активен</Badge>
                      )}
                    </td>
                    <td className={tableStyles.nowrap} title={formatDateTime(member.last_login_at)}>
                      {member.last_login_at ? formatRelative(member.last_login_at) : "не входил"}
                    </td>
                    <td className={tableStyles.actions}>
                      {self ? null : (
                        <MemberActions
                          member={member}
                          busy={update.isPending && update.variables.member.id === member.id}
                          onChange={(change) => update.mutate({ member, change })}
                          onEditWork={() =>
                            setEditingWork({
                              memberId: member.id,
                              name: memberName(member),
                              position: member.position,
                              departmentId: member.department_id,
                            })
                          }
                          onRemove={() => setRemoving(member)}
                        />
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
          {visible.length === 0 ? (
            <p className="muted" style={{ marginTop: 16 }}>
              Никого не найдено.
            </p>
          ) : null}
        </>
      )}
      <InvitesSection />
      {editingWork ? (
        <WorkEditDialog target={editingWork} onClose={() => setEditingWork(null)} />
      ) : null}

      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(open) => !open && setRemoving(null)}
        title="Убрать из компании?"
        description={
          removing
            ? `${memberName(removing)} сразу потеряет доступ к компании, диалоги здесь скроются и через 30 дней удалятся. Учётка kronto останется — вернуться можно по новому приглашению. Рабочее место освободится.`
            : undefined
        }
        confirmLabel="Убрать"
        onConfirm={async () => {
          if (!removing) return;
          await unwrap(
            api.DELETE("/api/v1/users/{user_id}", {
              params: { path: { user_id: removing.id } },
            }),
          );
          toast.show(`${memberName(removing)} больше не в компании`);
          await refresh();
        }}
      />
    </Page>
  );
}

function Person({
  member,
  self = false,
  children,
}: {
  member: Member;
  self?: boolean;
  children?: ReactNode;
}) {
  const name = memberName(member);
  return (
    <div className={styles.person}>
      <Avatar
        colorful
        name={name}
        initials={member.email ? personInitials(member.full_name, member.email) : "?"}
      />
      <div>
        <span className={styles.personName}>
          {name}
          {self ? <Badge>вы</Badge> : null}
        </span>
        {member.full_name && member.email ? (
          <span className={tableStyles.sub}>{member.email}</span>
        ) : null}
        {children}
      </div>
    </div>
  );
}

/** «Должность · Отдел» под именем; пусто — ничего. Неподтверждённый отдел
 * помечен: закрытые папки отдела человеку пока не открыты. */
function Work({
  position,
  department,
  unconfirmed = false,
}: {
  position: string | null;
  department?: string;
  unconfirmed?: boolean;
}) {
  const text = [position, department && unconfirmed ? `${department} (не подтверждён)` : department]
    .filter(Boolean)
    .join(" · ");
  return text ? <span className={tableStyles.sub}>{text}</span> : null;
}

function MemberActions({
  member,
  busy,
  onChange,
  onEditWork,
  onRemove,
}: {
  member: Member;
  busy: boolean;
  onChange: (change: Change) => void;
  onEditWork: () => void;
  onRemove: () => void;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <IconButton size="sm" label={`Действия: ${memberName(member)}`} disabled={busy}>
          <Ellipsis size={16} aria-hidden />
        </IconButton>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {/* Заблокированному должность не правят: в справочнике его нет. */}
        {member.status === "active" ? (
          <DropdownMenuItem
            icon={<BriefcaseBusiness size={16} aria-hidden />}
            onSelect={onEditWork}
          >
            Должность и отдел
          </DropdownMenuItem>
        ) : null}
        {member.role === "admin" ? (
          <DropdownMenuItem
            icon={<UserRound size={16} aria-hidden />}
            onSelect={() => onChange({ role: "employee" })}
          >
            Сделать сотрудником
          </DropdownMenuItem>
        ) : (
          <DropdownMenuItem
            icon={<ShieldCheck size={16} aria-hidden />}
            onSelect={() => onChange({ role: "admin" })}
          >
            Сделать администратором
          </DropdownMenuItem>
        )}
        {member.status === "blocked" ? (
          <DropdownMenuItem
            icon={<LockOpen size={16} aria-hidden />}
            onSelect={() => onChange({ blocked: false })}
          >
            Разблокировать
          </DropdownMenuItem>
        ) : (
          <DropdownMenuItem
            icon={<Ban size={16} aria-hidden />}
            onSelect={() => onChange({ blocked: true })}
          >
            Заблокировать
          </DropdownMenuItem>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem
          className={styles.menuDanger}
          icon={<UserMinus size={16} aria-hidden />}
          onSelect={onRemove}
        >
          Убрать из компании
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
