import { useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound } from "lucide-react";
import { useId, useState, type SubmitEvent } from "react";

import styles from "../admin/Admin.module.css";
import { ConfirmDialog } from "../admin/common";
import { api, unwrap, type Schemas } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { formatDate, formatDateTime, formatRelative } from "../lib/format";
import { useDocumentTitle } from "../lib/title";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Notice } from "../ui/Notice";
import pageStyles from "../ui/Page.module.css";
import { SkeletonList } from "../ui/Skeleton";
import tableStyles from "../ui/Table.module.css";
import { useToast } from "../ui/useToast";
import { STAFF_KEY } from "./keys";

type Person = Schemas["StaffPersonResponse"];
type Membership = Person["companies"][number];

/** Как на сервере: короче — пустой ответ, перечислять всех людей незачем. */
const MIN_QUERY = 3;
/** Сервер отдаёт не больше стольких учёток (SEARCH_LIMIT). */
const SEARCH_LIMIT = 20;

const ROLE_LABEL: Record<string, string> = { admin: "администратор", employee: "сотрудник" };
const STATUS_LABEL: Record<string, string> = {
  active: "активен",
  pending: "ждёт одобрения",
  blocked: "доступ закрыт",
  left: "ушёл из компании",
};

/** Имя в карточке: без имени — почта. */
function personName(person: Person): string {
  return person.full_name || person.email;
}

/** «приложение · ключи доступа: 2 · резервных кодов: 10» или «только код на почту». */
function factorsText(person: Person): string {
  const parts = [
    person.totp ? "приложение" : null,
    person.passkeys ? `ключи доступа: ${person.passkeys}` : null,
    person.backup_codes ? `резервных кодов: ${person.backup_codes}` : null,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : "только код на почту";
}

/**
 * Люди (ТЗ §9): помочь тому, кто не может войти. Учётку ищут по почте или
 * имени, видно, как человек входит и где состоит; пароль команда не видит
 * и не задаёт — только отправляет письмо со ссылкой, как «Забыли пароль?».
 */
export function PeopleTab() {
  useDocumentTitle("Люди");
  const hintId = useId();
  const errorId = useId();
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [short, setShort] = useState(false);
  const [resetting, setResetting] = useState<Person | null>(null);
  const toast = useToast();
  const queryClient = useQueryClient();

  const people = useQuery({
    queryKey: [...STAFF_KEY, "people", query],
    queryFn: () => unwrap(api.GET("/api/v1/staff/people", { params: { query: { q: query } } })),
    enabled: query.length >= MIN_QUERY,
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    const value = draft.trim();
    if (value.length < MIN_QUERY) {
      setShort(true);
      return;
    }
    setShort(false);
    // Тот же запрос ещё раз — перечитать: человек мог только что подтвердить почту.
    if (value === query) void people.refetch();
    else setQuery(value);
  }

  return (
    <>
      <div className={styles.tabHead}>
        <p className={styles.tabIntro}>
          Помочь тому, кто не может войти: найдите учётку по почте или имени, посмотрите, как
          человек входит, и при необходимости отправьте ссылку на новый пароль.
        </p>
      </div>
      <form role="search" className={styles.block} onSubmit={submit} noValidate>
        <div className={pageStyles.row}>
          <span className={styles.searchWrap}>
            <input
              className={styles.search}
              type="search"
              placeholder="Почта или имя"
              aria-label="Почта или имя"
              aria-describedby={short ? `${hintId} ${errorId}` : hintId}
              aria-invalid={short || undefined}
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value);
                if (short && e.target.value.trim().length >= MIN_QUERY) setShort(false);
              }}
            />
          </span>
          <Button type="submit" size="sm">
            Найти
          </Button>
        </div>
        <p id={hintId} className={tableStyles.sub} style={{ marginTop: 8 }}>
          Часть почты или имени — от {MIN_QUERY} символов.
        </p>
        {short ? (
          <p id={errorId} className={styles.errorText} role="alert" style={{ marginTop: 4 }}>
            Нужно хотя бы {MIN_QUERY} символа.
          </p>
        ) : null}
      </form>

      {query.length < MIN_QUERY ? null : people.isPending ? (
        <SkeletonList label="Ищем учётки" />
      ) : people.isError ? (
        <Notice kind="error">{errorMessage(people.error)}</Notice>
      ) : people.data.length === 0 ? (
        <p className="muted">Никого не нашли по запросу «{query}».</p>
      ) : (
        <>
          <ul className={styles.cards} aria-label="Найденные учётки">
            {people.data.map((person) => (
              <PersonCard key={person.id} person={person} onReset={() => setResetting(person)} />
            ))}
          </ul>
          {people.data.length >= SEARCH_LIMIT ? (
            <p className="muted" style={{ marginTop: 12 }}>
              Показаны первые {SEARCH_LIMIT} — уточните запрос.
            </p>
          ) : null}
        </>
      )}

      <ConfirmDialog
        open={resetting !== null}
        onOpenChange={(open) => !open && setResetting(null)}
        title="Отправить ссылку на новый пароль?"
        description={resetting ? resetDescription(resetting) : undefined}
        confirmLabel="Отправить"
        danger={false}
        onConfirm={async () => {
          if (!resetting) return;
          try {
            await unwrap(
              api.POST("/api/v1/staff/people/{account_id}/password-reset", {
                params: { path: { account_id: resetting.id } },
              }),
            );
          } catch (error) {
            if (error instanceof ApiError && error.status === 429) {
              throw new ApiError(429, "Слишком много писем за час — попробуйте позже", error.code);
            }
            throw error;
          }
          toast.show(`Ссылка на новый пароль отправлена на ${resetting.email}`);
          await queryClient.invalidateQueries({ queryKey: STAFF_KEY });
        }}
      />
    </>
  );
}

function resetDescription(person: Person): string {
  const strong = person.totp || person.passkeys > 0;
  return [
    `На ${person.email} придёт письмо со ссылкой — то же, что «Забыли пароль?».`,
    "Новый пароль человек задаст сам, команда его не увидит.",
    strong ? "При смене понадобится код из приложения или резервный код." : null,
    "После смены пароля все прежние входы закроются.",
  ]
    .filter(Boolean)
    .join(" ");
}

function PersonCard({ person, onReset }: { person: Person; onReset: () => void }) {
  const titleId = useId();
  return (
    <li className={styles.card} aria-labelledby={titleId}>
      <div className={styles.cardHead}>
        <div style={{ minWidth: 0 }}>
          <p id={titleId} className={styles.cardTitle} style={{ overflowWrap: "anywhere" }}>
            {personName(person)}
          </p>
          {person.full_name ? (
            <span className={tableStyles.sub} style={{ overflowWrap: "anywhere" }}>
              {person.email}
            </span>
          ) : null}
        </div>
        <span className={pageStyles.row} style={{ gap: 6 }}>
          {!person.email_verified ? <Badge tone="warn">почта не подтверждена</Badge> : null}
          {person.must_change_password ? <Badge>временный пароль</Badge> : null}
          {person.staff ? <Badge tone="accent">команда kronto</Badge> : null}
        </span>
      </div>
      <dl className={styles.kv}>
        <dt>Последний вход</dt>
        <dd title={person.last_login_at ? formatDateTime(person.last_login_at) : undefined}>
          {person.last_login_at ? formatRelative(person.last_login_at) : "не входил"}
        </dd>
        <dt>Второй фактор</dt>
        <dd>{factorsText(person)}</dd>
        <dt>Активных входов</dt>
        <dd className="num">{person.sessions}</dd>
        <dt>Учётка создана</dt>
        <dd>{formatDate(person.created_at)}</dd>
      </dl>
      {person.companies.length ? (
        <ul
          aria-label={`Компании: ${personName(person)}`}
          className={styles.samples}
          style={{ color: "var(--ink)" }}
        >
          {person.companies.map((item) => (
            <CompanyLine key={item.tenant_id} item={item} />
          ))}
        </ul>
      ) : (
        <p className="muted" style={{ fontSize: "var(--fs-small)" }}>
          Ни в одной компании не состоит.
        </p>
      )}
      <div className={styles.cardFoot}>
        <Button variant="ghost" size="xs" onClick={onReset}>
          <KeyRound size={16} aria-hidden /> Отправить ссылку на новый пароль
        </Button>
      </div>
    </li>
  );
}

function CompanyLine({ item }: { item: Membership }) {
  const parts = [
    item.company_name,
    ROLE_LABEL[item.role] ?? item.role,
    STATUS_LABEL[item.status] ?? item.status,
  ];
  return (
    <li>
      {parts.join(" · ")}
      <span
        className={tableStyles.sub}
        title={item.last_login_at ? formatDateTime(item.last_login_at) : undefined}
      >
        {item.last_login_at
          ? `вход в компанию ${formatRelative(item.last_login_at)}`
          : "в компанию не входил"}
      </span>
    </li>
  );
}
