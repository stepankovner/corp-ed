import ReactMarkdown, { type Components } from "react-markdown";
import { Link } from "react-router";
import remarkGfm from "remark-gfm";

import { editionDate, fillPlaceholders, LEGAL, type LegalDocument } from "./legal/documents";
import { SITE, SITE_URL, useSiteTitle, type SiteMeta } from "./meta";
import { DraftNote, SectionHead, SiteLayout } from "./SiteLayout";
import site from "./Site.module.css";

/**
 * Юридические страницы (ТЗ §11): проекты документов, которые проверяет
 * юрист, — с пометкой об этом. Тексты — в legal/*.md, реквизиты и версии —
 * в legal/documents.ts. Пока юрист не проверил тексты, на боевом домене
 * регистрация только по приглашению (REGISTRATION_ENABLED=false), а запись
 * на созвон выключена (LEADS_ENABLED=false).
 */
function LegalPage({ meta, title, doc }: { meta: SiteMeta; title: string; doc: LegalDocument }) {
  useSiteTitle(meta);
  return (
    <SiteLayout>
      <article className={site.narrow}>
        <SectionHead level={1} eyebrow="документы" title={title} />
        <div className={site.prose}>
          <DraftNote label="Проект — проверяется юристом.">
            Текст может измениться; реквизиты появятся после регистрации ИП.
          </DraftNote>
          <p className={site.updated}>
            Редакция от {editionDate(doc.edition)}
            {doc.version ? <span> · версия {doc.version}</span> : null}
          </p>
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={COMPONENTS}
            disallowedElements={["img"]}
            unwrapDisallowed
          >
            {fillPlaceholders(doc)}
          </ReactMarkdown>
        </div>
      </article>
    </SiteLayout>
  );
}

const COMPONENTS: Components = {
  // Свои страницы — ссылкой внутри сайта; чужие — в новой вкладке.
  a: ({ href, children }) => {
    const path = href?.startsWith(SITE_URL) ? href.slice(SITE_URL.length) || "/" : href;
    if (path?.startsWith("/")) return <Link to={path}>{children}</Link>;
    if (href?.startsWith("mailto:")) return <a href={href}>{children}</a>;
    return (
      <a href={href} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    );
  },
  // Широкая таблица прокручивается сама, а не страница.
  table: ({ children }) => (
    <div className={site.tableWrap}>
      <table>{children}</table>
    </div>
  ),
};

export function PrivacyPage() {
  return (
    <LegalPage
      meta={SITE.privacy}
      title="Политика обработки персональных данных"
      doc={LEGAL.privacy}
    />
  );
}

export function TermsPage() {
  return <LegalPage meta={SITE.terms} title="Пользовательское соглашение" doc={LEGAL.terms} />;
}

export function ConsentPage() {
  return (
    <LegalPage
      meta={SITE.consent}
      title="Согласие на обработку персональных данных"
      doc={LEGAL.consent}
    />
  );
}

export function CallConsentPage() {
  return (
    <LegalPage
      meta={SITE.consentCall}
      title="Согласие на обработку персональных данных для записи на созвон"
      doc={LEGAL.consentCall}
    />
  );
}

export function OfferPage() {
  return (
    <LegalPage
      meta={SITE.offer}
      title="Публичная оферта о предоставлении доступа к сервису kronto"
      doc={LEGAL.offer}
    />
  );
}

export function RefundPage() {
  return (
    <LegalPage meta={SITE.refund} title="Политика возврата денежных средств" doc={LEGAL.refund} />
  );
}

export function CookiesPage() {
  return <LegalPage meta={SITE.cookies} title="Cookies и данные в браузере" doc={LEGAL.cookies} />;
}
