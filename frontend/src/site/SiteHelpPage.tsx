import { Link } from "react-router";

import { HelpContent, SUPPORT } from "../help/HelpPage";
import styles from "../help/Help.module.css";
import { buttonClass } from "../ui/buttonClass";
import { CONTACTS, SITE, useSiteTitle } from "./meta";
import { SectionHead, SiteLayout } from "./SiteLayout";
import site from "./Site.module.css";

/**
 * «Помощь» для гостя (ТЗ §1): те же статьи, что в приложении, а вместо
 * формы обращения — как связаться без входа. Вошедший видит «Помощь»
 * приложения с формой.
 */
export function SiteHelpPage() {
  useSiteTitle(SITE.help);
  return (
    <SiteLayout>
      <div className={`${site.container} ${site.section}`}>
        <SectionHead
          level={1}
          eyebrow="помощь"
          title="Как устроен kronto"
          lead="Короткие ответы для сотрудников и администраторов. Не нашли нужного — напишите нам."
        />
        <HelpContent aside={<GuestSupport />} />
      </div>
    </SiteLayout>
  );
}

function GuestSupport() {
  return (
    <section id={SUPPORT} className={styles.support} aria-labelledby={`${SUPPORT}-title`}>
      <div>
        <h2 id={`${SUPPORT}-title`} className={styles.supportTitle} tabIndex={-1}>
          Написать в поддержку
        </h2>
        <p className={styles.supportText}>
          Работаете в kronto — войдите и напишите из «Помощи»: ответим на почту учётной записи. Ещё
          не с нами — напишите на почту или в Telegram.
        </p>
      </div>
      <Link to="/login?next=%2Fhelp%23support" className={buttonClass("dark", "sm", true)}>
        Войти и написать
      </Link>
      <p className={styles.supportText}>
        <a href={`mailto:${CONTACTS.email}`}>{CONTACTS.email}</a>
        <br />
        <a href={`https://t.me/${CONTACTS.telegram}`} rel="noopener noreferrer">
          Telegram @{CONTACTS.telegram}
        </a>
      </p>
    </section>
  );
}
