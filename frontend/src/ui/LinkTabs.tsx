import { useEffect, useRef } from "react";
import { Link, useLocation } from "react-router";

import styles from "./LinkTabs.module.css";

export interface LinkTab {
  to: string;
  label: string;
}

/**
 * Вкладки, у каждой — свой адрес: на вкладку можно дать ссылку, «назад»
 * возвращает на прежнюю. Открыта та, чей адрес — начало текущего.
 */
export function LinkTabs({ tabs, label }: { tabs: LinkTab[]; label: string }) {
  const { pathname } = useLocation();
  const active = useRef<HTMLAnchorElement>(null);

  // На телефоне лента вкладок шире экрана: открытая вкладка — в поле зрения.
  useEffect(() => {
    active.current?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [pathname]);

  return (
    <nav className={styles.tabs} aria-label={label}>
      {tabs.map((tab) => {
        const current = pathname === tab.to || pathname.startsWith(`${tab.to}/`);
        return (
          <Link
            key={tab.to}
            ref={current ? active : undefined}
            to={tab.to}
            className={styles.tab}
            aria-current={current ? "page" : undefined}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
