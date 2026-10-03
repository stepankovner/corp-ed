import { useMutation } from "@tanstack/react-query";
import { useState, type SubmitEvent } from "react";
import { Link } from "react-router";

import { api, unwrap } from "../api/client";
import { errorMessage } from "../api/errors";
import { useAuth, useMe } from "../auth/context";
import { useDocumentTitle } from "../lib/title";
import { Button } from "../ui/Button";
import { TextField } from "../ui/Field";
import { Notice } from "../ui/Notice";
import { useToast } from "../ui/useToast";
import { Section } from "./common";
import styles from "./Settings.module.css";

/** Как в схеме бэкенда (PersonName). */
const MAX_NAME = 100;

/** Пробелы по краям и двойные внутри сервер всё равно уберёт. */
function clean(value: string): string {
  return value.split(/\s+/).filter(Boolean).join(" ");
}

/**
 * Имя и фамилия обязательны (ТЗ §4). Отчество, фото, должность и
 * контакты — этапом 5, вместе со справочником сотрудников.
 */
export function ProfileTab() {
  useDocumentTitle("Профиль");
  const me = useMe();
  return (
    <div className={styles.stack}>
      {/* Ключ — сохранённое имя: после сохранения форма начинается с него. */}
      <NameSection key={`${me.first_name ?? ""}\n${me.last_name ?? ""}`} />
      <Section
        title="Почта"
        description="На неё приходят коды входа и письма о безопасности учётной записи."
      >
        <div className={styles.value}>
          <span className={styles.valueText}>{me.email}</span>
          <span className="muted">
            Сменить почту можно в разделе{" "}
            <Link to="/settings/account">«Управление учётной записью»</Link>.
          </span>
        </div>
      </Section>
    </div>
  );
}

function NameSection() {
  const me = useMe();
  const { reloadMe } = useAuth();
  const toast = useToast();
  const [first, setFirst] = useState(me.first_name ?? "");
  const [last, setLast] = useState(me.last_name ?? "");
  const [touched, setTouched] = useState(false);
  const firstName = clean(first);
  const lastName = clean(last);
  const changed = firstName !== (me.first_name ?? "") || lastName !== (me.last_name ?? "");

  const save = useMutation({
    mutationFn: () =>
      unwrap(
        api.PATCH("/api/v1/account", { body: { first_name: firstName, last_name: lastName } }),
      ),
    onSuccess: async () => {
      await reloadMe();
      toast.show("Имя сохранено");
    },
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    setTouched(true);
    if (!firstName || !lastName) return;
    save.mutate();
  }

  return (
    <Section
      title="Имя и фамилия"
      description="Так вас видят коллеги и администраторы ваших компаний."
    >
      <form className={styles.form} onSubmit={submit} noValidate>
        {save.isError ? <Notice kind="error">{errorMessage(save.error)}</Notice> : null}
        <div className={styles.grid2}>
          <TextField
            label="Имя"
            autoComplete="given-name"
            required
            maxLength={MAX_NAME}
            value={first}
            onChange={(e) => setFirst(e.target.value)}
            error={touched && !firstName ? "Укажите имя." : null}
          />
          <TextField
            label="Фамилия"
            autoComplete="family-name"
            required
            maxLength={MAX_NAME}
            value={last}
            onChange={(e) => setLast(e.target.value)}
            error={touched && !lastName ? "Укажите фамилию." : null}
          />
        </div>
        <div className={styles.actions}>
          <Button type="submit" size="sm" busy={save.isPending} disabled={!changed}>
            Сохранить
          </Button>
        </div>
      </form>
    </Section>
  );
}
