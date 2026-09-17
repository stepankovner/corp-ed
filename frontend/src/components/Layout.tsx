import { NavLink, Outlet, useLocation } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import styles from "./Layout.module.css";

const ROLE_LABELS = {
  manager: "Руководитель",
  intern: "Стажёр",
} as const;

export function Layout() {
  const { user, logout } = useAuth();
  const location = useLocation();
  const isManager = user?.role === "manager";

  // Программа открывается по своему адресу, но остаётся тем же разделом:
  // иначе при переходе к результату подсветка в меню гаснет.
  const inPrograms =
    location.pathname.startsWith("/briefs") ||
    location.pathname.startsWith("/programs");

  const linkClass = ({ isActive }: { isActive: boolean }) =>
    `${styles.link} ${isActive ? styles.active : ""}`;

  return (
    <div className={styles.shell}>
      <aside className={styles.sidebar}>
        <NavLink to="/" className={styles.brand}>
          kronto
          <span className={styles.brandNote}>адаптация стажёров</span>
        </NavLink>

        <nav className={styles.nav}>
          {isManager ? (
            <>
              <NavLink to="/materials" className={linkClass}>
                Материалы
              </NavLink>
              <NavLink
                to="/briefs"
                className={`${styles.link} ${inPrograms ? styles.active : ""}`}
              >
                Программы
              </NavLink>
            </>
          ) : null}
          <NavLink to="/chat" className={linkClass}>
            Вопросы
          </NavLink>
        </nav>

        <div className={styles.user}>
          {user ? (
            <>
              <span className={styles.userName}>
                {user.full_name ?? user.email}
              </span>
              <span className={styles.userRole}>{ROLE_LABELS[user.role]}</span>
            </>
          ) : null}
          <button type="button" className={styles.logout} onClick={logout}>
            Выйти
          </button>
        </div>
      </aside>

      <main className={styles.main}>
        <Outlet />
      </main>
    </div>
  );
}
