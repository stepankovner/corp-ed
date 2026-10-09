import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { isAdmin, useMe } from "../auth/context";
import { useDocumentTitle } from "../lib/title";
import { Badge } from "../ui/Badge";
import { Notice } from "../ui/Notice";
import { SkeletonList } from "../ui/Skeleton";
import { Switch } from "../ui/Switch";
import { useToast } from "../ui/useToast";
import { Section } from "./common";
import styles from "./Settings.module.css";

type Settings = Schemas["NotificationSettingsResponse"];
type Flag = keyof Settings;

/** Письма администратору — настройка участника в компании, ключ не про учётку. */
const SETTINGS_KEY = ["notifications", "settings"] as const;

const FLAGS: { flag: Flag; label: string; hint: string }[] = [
  {
    flag: "email_connectors",
    label: "Остановилось подключение",
    hint: "Источник перестал отдавать документы — нужно ввести доступ заново.",
  },
  {
    flag: "email_credits",
    label: "Кредиты",
    hint: "Израсходовано 80 % месячного пула, кредиты закончились или зачислены, сотрудники просят пополнить; счета, оплата, акты и напоминания об оплате.",
  },
  {
    flag: "email_join_requests",
    label: "Заявки",
    hint: "Человек просится в компанию или указал отдел с закрытыми папками — ждёт вашего решения.",
  },
  {
    flag: "email_weekly_digest",
    label: "Недельная сводка",
    hint: "По понедельникам: вопросы, частые темы, пробелы в документах.",
  },
];

/**
 * Уведомления (ТЗ §8). Колокольчик показывает события всегда, здесь —
 * письма: администратор выбирает, о чём ещё написать на почту. Письма о
 * безопасности учётки приходят всем и не выключаются.
 */
export function NotificationsTab() {
  useDocumentTitle("Уведомления");
  const me = useMe();
  return (
    <div className={styles.stack}>
      {isAdmin(me) ? (
        <AdminEmails email={me.email} />
      ) : (
        <p className={styles.lead}>
          Письма об остановленных подключениях, лимите вопросов и заявках на вступление получают
          администраторы компании.
        </p>
      )}
      <Section
        title="Письма о безопасности"
        description="Приходят всегда, отключить их нельзя: так вы сразу узнаете, если в учётную запись вошёл кто-то другой."
        aside={<Badge>всегда включены</Badge>}
      >
        <ul className={styles.bullets} aria-label="Когда приходят">
          <li>вход с нового устройства;</li>
          <li>смена пароля;</li>
          <li>смена почты.</li>
        </ul>
      </Section>
    </div>
  );
}

function AdminEmails({ email }: { email: string }) {
  // Только администратору: сотруднику сервер ответит 403.
  const settings = useQuery({
    queryKey: SETTINGS_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/notifications/settings")),
  });
  return (
    <Section
      title="Письма администратору"
      description={`Колокольчик в панели показывает все события. Здесь — о каких ещё написать на ${email}.`}
    >
      {settings.isPending ? (
        <SkeletonList rows={4} label="Загружаем настройки" />
      ) : settings.isError ? (
        <Notice kind="error">{errorMessage(settings.error)}</Notice>
      ) : (
        FLAGS.map((item) => (
          <FlagSwitch key={item.flag} {...item} saved={settings.data[item.flag]} />
        ))
      )}
    </Section>
  );
}

/**
 * Переключатель письма: PUT сразу, только этот флаг. Общая очередь (scope)
 * — быстрые щелчки доходят до сервера по порядку; пока запрос в пути,
 * показываем выбранное, а не прежнее.
 */
function FlagSwitch({
  flag,
  label,
  hint,
  saved,
}: {
  flag: Flag;
  label: string;
  hint: string;
  saved: boolean;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const save = useMutation({
    scope: { id: "notification-settings" },
    mutationFn: (checked: boolean) => {
      const body: Schemas["NotificationSettingsRequest"] = {};
      body[flag] = checked;
      return unwrap(api.PUT("/api/v1/notifications/settings", { body }));
    },
    onSuccess: (data) => {
      queryClient.setQueryData(SETTINGS_KEY, data);
      toast.show("Сохранено");
    },
    onError: (error) => toast.show(errorMessage(error), { tone: "error" }),
  });
  const checked = save.isPending ? save.variables : saved;
  return (
    <Switch
      label={label}
      hint={hint}
      checked={checked}
      onCheckedChange={(next) => save.mutate(next)}
    />
  );
}
