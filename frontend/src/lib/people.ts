/** Отображение профиля (ТЗ §4): телефон, Telegram, имя полностью. */

/** +79991234567 → +7 999 123-45-67; другие страны — как хранится. */
export function formatPhone(phone: string): string {
  const ru = /^\+7(\d{3})(\d{3})(\d{2})(\d{2})$/.exec(phone);
  if (ru) return `+7 ${ru[1]} ${ru[2]}-${ru[3]}-${ru[4]}`;
  return phone;
}

export function telegramUrl(username: string): string {
  return `https://t.me/${encodeURIComponent(username)}`;
}

interface Named {
  first_name: string | null;
  last_name: string | null;
  patronymic?: string | null;
  email: string;
}

/** «Смирнова Анна Сергеевна» — для карточки; без имени — почта. */
export function fullNameWithPatronymic(person: Named): string {
  const parts = [person.last_name, person.first_name, person.patronymic].filter(Boolean);
  return parts.length ? parts.join(" ") : person.email;
}

/** «Анна Смирнова» — для списков; без имени — почта. */
export function shortName(person: Named): string {
  const parts = [person.first_name, person.last_name].filter(Boolean);
  return parts.length ? parts.join(" ") : person.email;
}
