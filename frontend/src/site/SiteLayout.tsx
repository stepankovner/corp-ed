import { Menu, X } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, useLocation } from "react-router";

import { useAuth } from "../auth/context";
import { ThemeMenu } from "../layout/ThemeOptions";
import { buttonClass } from "../ui/buttonClass";
import { IconButton } from "../ui/IconButton";
import { Logo } from "../ui/Logo";
import { CONTACTS } from "./meta";
import styles from "./Site.module.css";

const NAV = [
  { to: "/demo", label: "Песочница" },
  { to: "/security", label: "Безопасность" },
  { to: "/pricing", label: "Тарифы" },
  { to: "/help", label: "Помощь" },
  { to: "/about", label: "О компании" },
] as const;

const FOOTER = [
  {
    title: "Продукт",
    links: [
      { to: "/", label: "Главная" },
      { to: "/demo", label: "Песочница" },
      { to: "/pricing", label: "Тарифы" },
      { to: "/security", label: "Безопасность и данные" },
    ],
  },
  {
    title: "Компания",
    links: [
      { to: "/about", label: "О компании" },
      { to: "/help", label: "Помощь" },
      { to: "/pricing/request", label: "Записаться на созвон" },
    ],
  },
  {
    title: "Документы",
    links: [
      { to: "/privacy", label: "Политика обработки персональных данных" },
      { to: "/terms", label: "Пользовательское соглашение" },
      { to: "/consent", label: "Согласие на обработку данных" },
    ],
  },
] as const;

/** Была ли уже отрисована страница сайта в этой вкладке. */
let navigated = false;

/**
 * Оболочка публичного сайта (ТЗ §1): шапка с разделами и входом, подвал
 * со ссылками и контактами. Страницы сайта отдаются готовым HTML
 * (prerender.tsx) — здесь ничего не должно зависеть от браузера при
 * первой отрисовке.
 */
export function SiteLayout({ children }: { children: ReactNode }) {
  const { state } = useAuth();
  const { pathname, hash } = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const signedIn = state.status === "authenticated";

  // Новая страница сайта — с начала, ссылка на раздел (#demo) — к нему.
  // Первую страницу после загрузки прокручивает браузер: готовый HTML мог
  // уже прокрутиться, пока грузились скрипты.
  useEffect(() => {
    const first = !navigated;
    navigated = true;
    const target = hash ? document.getElementById(decodeURIComponent(hash.slice(1))) : null;
    if (target) target.scrollIntoView();
    else if (!first) window.scrollTo(0, 0);
  }, [pathname, hash]);

  return (
    <div className={styles.site}>
      <a className="skip-link" href="#content">
        К содержанию
      </a>
      <header className={styles.header}>
        <div className={styles.headerInner}>
          <Link to="/" className={styles.brand} aria-label="kronto — на главную">
            <Logo height={24} />
          </Link>
          <nav aria-label="Разделы сайта" className={styles.nav}>
            <ul className={styles.navList}>
              {NAV.map((item) => (
                <li key={item.to}>
                  <NavLink
                    to={item.to}
                    className={({ isActive }) =>
                      [styles.navLink, isActive ? styles.navActive : ""].join(" ")
                    }
                  >
                    {item.label}
                  </NavLink>
                </li>
              ))}
            </ul>
          </nav>
          <div className={styles.headerActions}>
            <ThemeMenu />
            {signedIn ? (
              <Link to="/" className={buttonClass("dark", "sm")}>
                Открыть kronto
              </Link>
            ) : (
              <>
                <Link to="/login" className={buttonClass("ghost", "sm")}>
                  Войти
                </Link>
                <Link
                  to="/pricing/request"
                  className={`${buttonClass("dark", "sm")} ${styles.headerCta}`}
                >
                  Записаться на созвон
                </Link>
              </>
            )}
            <IconButton
              label={menuOpen ? "Закрыть меню" : "Меню"}
              className={styles.menuButton}
              aria-expanded={menuOpen}
              aria-controls="site-menu"
              tooltip={false}
              onClick={() => setMenuOpen((open) => !open)}
            >
              {menuOpen ? <X size={20} aria-hidden /> : <Menu size={20} aria-hidden />}
            </IconButton>
          </div>
        </div>
        <nav
          id="site-menu"
          aria-label="Разделы сайта"
          className={styles.mobileNav}
          hidden={!menuOpen}
        >
          <ul>
            {NAV.map((item) => (
              <li key={item.to}>
                <Link to={item.to} onClick={() => setMenuOpen(false)}>
                  {item.label}
                </Link>
              </li>
            ))}
            {signedIn ? null : (
              <li>
                <Link to="/pricing/request" onClick={() => setMenuOpen(false)}>
                  Записаться на созвон
                </Link>
              </li>
            )}
          </ul>
        </nav>
      </header>

      <main id="content" className={styles.main} tabIndex={-1}>
        {children}
      </main>

      <footer className={styles.footer}>
        <div className={styles.footerInner}>
          <div className={styles.footerBrand}>
            <Logo height={22} />
            <p className="muted">Ответы по документам компании со ссылкой на источник.</p>
            <p className={styles.footerContacts}>
              <a href={`mailto:${CONTACTS.email}`}>{CONTACTS.email}</a>
              <a href={`https://t.me/${CONTACTS.telegram}`} rel="noopener noreferrer">
                Telegram @{CONTACTS.telegram}
              </a>
            </p>
          </div>
          {FOOTER.map((group) => (
            <nav key={group.title} aria-label={group.title} className={styles.footerGroup}>
              <p className={styles.footerTitle}>{group.title}</p>
              <ul>
                {group.links.map((link) => (
                  <li key={link.to}>
                    <Link to={link.to}>{link.label}</Link>
                  </li>
                ))}
              </ul>
            </nav>
          ))}
        </div>
        <p className={styles.copyright}>© 2026 kronto</p>
      </footer>
    </div>
  );
}

/** Заголовок раздела страницы: подпись моноширинным, крупный заголовок, вводный абзац. */
export function SectionHead({
  eyebrow,
  title,
  lead,
  id,
  level = 2,
}: {
  eyebrow?: string;
  title: ReactNode;
  lead?: ReactNode;
  id?: string;
  level?: 1 | 2;
}) {
  const Heading = level === 1 ? "h1" : "h2";
  return (
    <div className={styles.sectionHead}>
      {eyebrow ? <p className={`mono ${styles.eyebrow}`}>{eyebrow}</p> : null}
      <Heading id={id} className={level === 1 ? styles.pageTitle : styles.sectionTitle}>
        {title}
      </Heading>
      {lead ? <p className={styles.lead}>{lead}</p> : null}
    </div>
  );
}

/** Черновой текст до замены командой: заметная пометка «заменить» (ТЗ §11). */
export function DraftNote({ children }: { children: ReactNode }) {
  return (
    <p className={styles.draft} role="note">
      <strong>Черновик — заменить.</strong> {children}
    </p>
  );
}
