import { Link2, MessageSquareText, Settings2 } from "lucide-react";
import { NavLink, Outlet } from "react-router";

import { useMe } from "../auth/context";
import { ChatProvider } from "../chat/ChatProvider";
import { Logo } from "../ui/Logo";
import styles from "./AppShell.module.css";
import { UsageBanner } from "./UsageBanner";
import { UserMenu } from "./UserMenu";

function navClass({ isActive }: { isActive: boolean }) {
  return isActive ? `${styles.navLink} ${styles.active}` : styles.navLink;
}

export function AppShell() {
  const me = useMe();
  return (
    <ChatProvider userId={me.id}>
      <div className={styles.shell}>
        <a className="skip-link" href="#main">
          К содержимому
        </a>
        <header className={styles.header}>
          <div className={styles.inner}>
            <NavLink to="/" className={styles.brand} aria-label="Kronto — к вопросам">
              <Logo height={22} />
              <span className={styles.company}>{me.company_name}</span>
            </NavLink>
            <nav className={styles.nav} aria-label="Разделы">
              <NavLink to="/" end className={navClass}>
                <MessageSquareText size={18} aria-hidden />
                <span className={styles.navLabel}>Вопросы</span>
              </NavLink>
              <NavLink to="/sources" className={navClass}>
                <Link2 size={18} aria-hidden />
                <span className={styles.navLabel}>Мои источники</span>
              </NavLink>
              {me.role === "admin" ? (
                <NavLink to="/admin" className={navClass}>
                  <Settings2 size={18} aria-hidden />
                  <span className={styles.navLabel}>Управление</span>
                </NavLink>
              ) : null}
            </nav>
            <UserMenu />
          </div>
        </header>
        {me.role === "admin" ? <UsageBanner /> : null}
        <main className={styles.main} id="main">
          <Outlet />
        </main>
      </div>
    </ChatProvider>
  );
}
