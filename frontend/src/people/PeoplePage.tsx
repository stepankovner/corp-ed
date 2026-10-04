import { useQuery } from "@tanstack/react-query";
import { Mail, Phone, Search, Send, UsersRound } from "lucide-react";
import { useDeferredValue, useMemo, useState } from "react";
import { Link } from "react-router";

import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { isAdmin, useCompany, useMe } from "../auth/context";
import { personInitials } from "../lib/initials";
import { formatPhone, fullNameWithPatronymic, shortName, telegramUrl } from "../lib/people";
import { plural } from "../lib/format";
import { useDocumentTitle } from "../lib/title";
import { Avatar } from "../ui/Avatar";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Select } from "../ui/Field";
import { Modal } from "../ui/Modal";
import { Notice } from "../ui/Notice";
import { EmptyState, Page, PageHeader } from "../ui/Page";
import { SkeletonList } from "../ui/Skeleton";
import { DEPARTMENTS_KEY, PEOPLE_KEY } from "./keys";
import styles from "./People.module.css";
import { WorkEditDialog, type WorkTarget } from "./WorkEditDialog";

type Person = Schemas["PersonResponse"];
const NO_DEPARTMENT = "none";

/** Поиск без учёта регистра и «ё»: «Семён» находится по «семен». */
function normalize(value: string): string {
  return value.toLowerCase().replace(/ё/g, "е");
}

function matches(person: Person, query: string): boolean {
  if (!query) return true;
  const haystack = normalize(
    [
      person.first_name,
      person.last_name,
      person.patronymic,
      person.email,
      person.position,
      person.department?.name,
      person.phone,
      person.telegram,
    ]
      .filter(Boolean)
      .join(" "),
  );
  return normalize(query)
    .split(/\s+/)
    .filter(Boolean)
    .every((word) => haystack.includes(word));
}

/**
 * Справочник коллег (ТЗ §4): кто работает в компании, должность, отдел и
 * контакты. Видят только люди этой компании.
 */
export function PeoplePage() {
  useDocumentTitle("Коллеги");
  const company = useCompany();
  const [query, setQuery] = useState("");
  const [department, setDepartment] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);
  const deferredQuery = useDeferredValue(query);
  const people = useQuery({
    queryKey: PEOPLE_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/people")),
  });
  const departments = useQuery({
    queryKey: DEPARTMENTS_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/departments")),
  });

  const shown = useMemo(
    () =>
      (people.data ?? []).filter(
        (person) =>
          matches(person, deferredQuery) &&
          (!department ||
            (department === NO_DEPARTMENT
              ? person.department === null
              : person.department?.id === department)),
      ),
    [people.data, deferredQuery, department],
  );
  const open = people.data?.find((person) => person.member_id === openId) ?? null;
  const total = people.data?.length ?? 0;

  return (
    <Page>
      <PageHeader
        label="компания"
        title="Коллеги"
        description={
          people.data
            ? `${total} ${plural(total, "человек", "человека", "человек")} в «${company.name}». Профиль видят только коллеги по компании.`
            : `Люди «${company.name}», их должности, отделы и контакты.`
        }
      />
      <div className={styles.toolbar} role="search">
        <label className={styles.search}>
          <Search size={18} aria-hidden className={styles.searchIcon} />
          <span className="visually-hidden">Поиск по имени, должности, отделу</span>
          <input
            type="search"
            className={styles.searchInput}
            placeholder="Имя, должность, отдел"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        {departments.data && departments.data.length > 0 ? (
          <Select
            aria-label="Отдел"
            value={department}
            onChange={(e) => setDepartment(e.target.value)}
          >
            <option value="">Все отделы</option>
            {departments.data.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
            <option value={NO_DEPARTMENT}>Без отдела</option>
          </Select>
        ) : null}
      </div>

      {people.isPending ? (
        <SkeletonList rows={6} label="Загрузка коллег" />
      ) : people.isError ? (
        <Notice kind="error">{errorMessage(people.error)}</Notice>
      ) : shown.length === 0 ? (
        <EmptyState icon={<UsersRound size={32} aria-hidden />} title="Никого не нашли">
          <p>Проверьте написание или выберите другой отдел.</p>
        </EmptyState>
      ) : (
        <>
          <p className="visually-hidden" aria-live="polite">
            {`Найдено: ${shown.length}`}
          </p>
          <ul className={styles.grid} aria-label="Коллеги">
            {shown.map((person) => (
              <li key={person.member_id}>
                <button
                  type="button"
                  className={styles.card}
                  onClick={() => setOpenId(person.member_id)}
                >
                  <Avatar
                    size="lg"
                    colorful
                    name={shortName(person)}
                    src={person.avatar_url}
                    initials={personInitials(person.full_name, person.email)}
                  />
                  <span className={styles.cardText}>
                    <span className={styles.name}>{shortName(person)}</span>
                    {person.position ? (
                      <span className={styles.meta}>{person.position}</span>
                    ) : null}
                    {person.department ? (
                      <span className={`mono ${styles.department}`}>{person.department.name}</span>
                    ) : null}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
      {open ? <PersonDialog person={open} onClose={() => setOpenId(null)} /> : null}
    </Page>
  );
}

function PersonDialog({ person, onClose }: { person: Person; onClose: () => void }) {
  const me = useMe();
  const company = useCompany();
  const [editing, setEditing] = useState<WorkTarget | null>(null);
  const itsMe = person.member_id === company.member_id;
  const work = [person.position, person.department?.name].filter(Boolean).join(" · ");

  if (editing) return <WorkEditDialog target={editing} onClose={() => setEditing(null)} />;
  return (
    <Modal
      open
      onOpenChange={(value) => !value && onClose()}
      title={fullNameWithPatronymic(person)}
      description={work || undefined}
      footer={
        itsMe ? (
          <Link to="/settings/profile" className={styles.footerLink}>
            Изменить свой профиль
          </Link>
        ) : isAdmin(me) ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={() =>
              setEditing({
                memberId: person.member_id,
                name: shortName(person),
                position: person.position,
                departmentId: person.department?.id ?? null,
              })
            }
          >
            Поправить должность и отдел
          </Button>
        ) : undefined
      }
    >
      <div className={styles.profile}>
        <Avatar
          size="xl"
          colorful
          name={shortName(person)}
          src={person.avatar_url}
          initials={personInitials(person.full_name, person.email)}
        />
        <div className={styles.badges}>
          {person.role === "admin" ? <Badge tone="accent">администратор</Badge> : null}
          {itsMe ? <Badge>это вы</Badge> : null}
        </div>
      </div>
      <ul className={styles.contacts} aria-label="Контакты">
        <li>
          <Mail size={18} aria-hidden />
          <a href={`mailto:${person.email}`}>{person.email}</a>
        </li>
        {person.phone ? (
          <li>
            <Phone size={18} aria-hidden />
            <a href={`tel:${person.phone}`}>{formatPhone(person.phone)}</a>
          </li>
        ) : null}
        {person.telegram ? (
          <li>
            <Send size={18} aria-hidden />
            <a href={telegramUrl(person.telegram)} target="_blank" rel="noreferrer">
              @{person.telegram}
            </a>
          </li>
        ) : null}
      </ul>
    </Modal>
  );
}
