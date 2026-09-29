import type { ReactNode } from "react";

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
        <Logo height={24} />
      </header>
      <main className={styles.center}>
        <div className={styles.glass}>
          <section className={styles.window} aria-labelledby="auth-title">
            <div className={styles.bar}>
              <WindowMark />
              {bar}
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
        Kronto — ответы по документам компании со ссылкой на источник
      </footer>
    </div>
  );
}
