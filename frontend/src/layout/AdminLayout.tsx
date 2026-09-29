import {
  BookA,
  ChartColumn,
  FileText,
  Plug,
  ScrollText,
  SearchX,
  Users,
  type LucideIcon,
} from "lucide-react";
import { NavLink, Outlet } from "react-router";

import styles from "./AdminLayout.module.css";

const SECTIONS: { to: string; label: string; icon: LucideIcon }[] = [
  { to: "documents", label: "Документы", icon: FileText },
  { to: "connectors", label: "Подключения", icon: Plug },
  { to: "users", label: "Сотрудники", icon: Users },
  { to: "gaps", label: "Пробелы в документах", icon: SearchX },
  { to: "glossary", label: "Глоссарий", icon: BookA },
  { to: "usage", label: "Лимит вопросов", icon: ChartColumn },
  { to: "audit", label: "Журнал действий", icon: ScrollText },
];

export function AdminLayout() {
  return (
    <div className={styles.layout}>
      <aside className={styles.sidebar}>
        <p className={`mono ${styles.heading}`}>управление</p>
        <nav aria-label="Управление">
          <ul className={styles.list}>
            {SECTIONS.map(({ to, label, icon: Icon }) => (
              <li key={to}>
                <NavLink
                  to={to}
                  className={({ isActive }) =>
                    isActive ? `${styles.link} ${styles.active}` : styles.link
                  }
                >
                  <Icon size={18} aria-hidden />
                  {label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
      </aside>
      <div className={styles.content}>
        <Outlet />
      </div>
    </div>
  );
}
