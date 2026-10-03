import { ShieldCheck } from "lucide-react";
import { Link } from "react-router";

import { useCompany } from "../auth/context";
import { useDocumentTitle } from "../lib/title";
import { buttonClass } from "../ui/buttonClass";
import { EmptyState, Page } from "../ui/Page";

/**
 * Администратору (или всем в компании с таким правилом) нужен надёжный
 * второй фактор — приложение или ключ доступа (ТЗ §3). Пока его нет,
 * данные компании сервер не отдаёт.
 */
export function StrongFactorRequired() {
  useDocumentTitle("Защита входа");
  const company = useCompany();
  return (
    <Page>
      <h1 className="visually-hidden">Защита входа</h1>
      <EmptyState icon={<ShieldCheck size={32} aria-hidden />} title="Сначала защитите вход">
        <p>
          {company.role === "admin"
            ? `Вы администратор «${company.name}»: у вас доступ к документам и людям компании. `
            : `В «${company.name}» вход защищают приложением или ключом доступа. `}
          Подключите приложение-аутентификатор (Яндекс Ключ, Google Authenticator) или ключ доступа
          — это займёт пару минут.
        </p>
        <Link to="/settings/security" className={buttonClass("dark", "md", false)}>
          Настроить защиту
        </Link>
      </EmptyState>
    </Page>
  );
}
