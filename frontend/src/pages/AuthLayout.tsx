import type { ReactNode } from "react";

import { ThemeMenu } from "../layout/ThemeOptions";
import { Logo, WindowMark } from "../ui/Logo";
import styles from "./AuthLayout.module.css";

export function AuthLayout({
  bar,
  title,
  lead,
  children,
}: {
  bar: ReactNode;
  title: ReactNode;
  lead?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className={styles.screen}>
      <header className={styles.top}>
        <Logo height={22} />
        <ThemeMenu />
      </header>
      <main className={styles.center}>
        <div className={styles.glass}>
          <section className={styles.window} aria-labelledby="auth-title">
            <div className={styles.bar}>
              <WindowMark />
              <span className={styles.barText}>{bar}</span>
            </div>
            <div className={styles.body}>
              <div>
                <h1 className={styles.title} id="auth-title">
                  {title}
                </h1>
                {lead ? <p className={styles.lead}>{lead}</p> : null}
              </div>
              {children}
            </div>
          </section>
        </div>
      </main>
      <footer className={styles.foot}>
        kronto — ответы по документам компании со ссылкой на источник
      </footer>
    </div>
  );
}
