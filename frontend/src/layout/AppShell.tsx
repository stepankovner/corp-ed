import { Menu } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, Outlet, useLocation } from "react-router";

import { isAdmin, needsStrongFactor, useMe } from "../auth/context";
import { ChatProvider } from "../chat/ChatProvider";
import { MOBILE_QUERY, useMediaQuery } from "../lib/media";
import { IconButton } from "../ui/IconButton";
import { Logo } from "../ui/Logo";
import styles from "./AppShell.module.css";
import { NewDialogButton, Sidebar } from "./Sidebar";
import { UsageBanner } from "./UsageBanner";

const COLLAPSED_KEY = "kronto.sidebar";

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === "collapsed";
  } catch {
    return false;
  }
}

function saveCollapsed(collapsed: boolean): void {
  try {
    if (collapsed) localStorage.setItem(COLLAPSED_KEY, "collapsed");
    else localStorage.removeItem(COLLAPSED_KEY);
  } catch {
    // Не запомнили — после перезагрузки панель снова развёрнута.
  }
}

const FOCUSABLE = 'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function AppShell() {
  const me = useMe();
  // Переписка — своя у каждой компании человека: другая компания — другой ключ.
  const chatKey = `${me.id}:${me.company?.tenant_id ?? "none"}`;
  return (
    <ChatProvider key={chatKey}>
      <Shell />
    </ChatProvider>
  );
}

function Shell() {
  const me = useMe();
  const mobile = useMediaQuery(MOBILE_QUERY);
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const sidebar = useRef<HTMLDivElement>(null);
  const burger = useRef<HTMLButtonElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);

  // Меню закрывается при переходе (в том числе «назад») и на широком экране.
  const { pathname } = useLocation();
  const [shownPath, setShownPath] = useState(pathname);
  if (shownPath !== pathname) {
    setShownPath(pathname);
    setDrawerOpen(false);
  }
  if (drawerOpen && !mobile) setDrawerOpen(false);

  const toggleCollapsed = useCallback(() => {
    const next = !collapsed;
    setCollapsed(next);
    saveCollapsed(next);
  }, [collapsed]);
  const closeDrawer = useCallback(() => setDrawerOpen(false), []);

  // Открыли — фокус на «Закрыть»; Esc закрывает, Tab не выходит из меню.
  useEffect(() => {
    if (!drawerOpen) return;
    closeButton.current?.focus();
    function onKey(event: KeyboardEvent) {
      // Esc в открытом меню учётной записи закроет сначала его (Radix).
      if (event.key === "Escape" && !event.defaultPrevented) {
        setDrawerOpen(false);
        return;
      }
      if (event.key !== "Tab" || !sidebar.current) return;
      const items = Array.from(sidebar.current.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (node) => node.offsetParent !== null,
      );
      const first = items[0];
      const last = items.at(-1);
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [drawerOpen]);

  // Закрыли — фокус, оставшийся в спрятанном меню, возвращается к кнопке меню.
  const wasOpen = useRef(false);
  useEffect(() => {
    if (drawerOpen) {
      wasOpen.current = true;
      return;
    }
    if (!wasOpen.current) return;
    wasOpen.current = false;
    const active = document.activeElement;
    if (!active || active === document.body || sidebar.current?.contains(active)) {
      burger.current?.focus();
    }
  }, [drawerOpen]);

  const mode = mobile ? "drawer" : collapsed ? "collapsed" : "expanded";
  return (
    <div
      className={[
        styles.shell,
        mode === "collapsed" ? styles.collapsed : "",
        mobile ? styles.mobile : "",
        drawerOpen ? styles.open : "",
      ].join(" ")}
    >
      <a className="skip-link" href="#main">
        К содержимому
      </a>
      <div
        ref={sidebar}
        id="app-sidebar"
        className={styles.sidebar}
        inert={mobile && !drawerOpen}
        {...(mobile && drawerOpen
          ? { role: "dialog", "aria-modal": true, "aria-label": "Меню" }
          : {})}
      >
        <Sidebar
          mode={mode}
          onToggle={toggleCollapsed}
          onClose={closeDrawer}
          closeRef={closeButton}
        />
      </div>
      {mobile && drawerOpen ? (
        <div className={styles.scrim} onClick={closeDrawer} aria-hidden />
      ) : null}
      <div className={styles.content} inert={mobile && drawerOpen}>
        {mobile ? (
          <header className={styles.topbar}>
            <IconButton
              ref={burger}
              label="Открыть меню"
              tooltip={false}
              aria-expanded={drawerOpen}
              aria-controls="app-sidebar"
              onClick={() => setDrawerOpen(true)}
            >
              <Menu size={20} aria-hidden />
            </IconButton>
            <Link to="/" className={styles.topbarBrand} aria-label="kronto — к вопросам">
              <Logo height={18} />
            </Link>
            <NewDialogButton compact />
          </header>
        ) : null}
        {isAdmin(me) && !needsStrongFactor(me) ? <UsageBanner /> : null}
        <main className={styles.main} id="main" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
