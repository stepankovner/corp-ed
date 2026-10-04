import { ChevronDown, Link2, Search, SearchX } from "lucide-react";
import { useEffect, useId, useState } from "react";
import { Link, useLocation } from "react-router";

import { plural } from "../lib/format";
import { useDocumentTitle } from "../lib/title";
import { Button } from "../ui/Button";
import { buttonClass } from "../ui/buttonClass";
import { EmptyState, Page, PageHeader } from "../ui/Page";
import { useToast } from "../ui/useToast";
import { ARTICLES, type Article, type Audience } from "./articles";
import styles from "./Help.module.css";
import { matches, searchable, textOf } from "./search";
import { SupportSection } from "./SupportSection";

const SUPPORT = "support";

const GROUPS: { audience: Audience; title: string; note?: string }[] = [
  { audience: "employee", title: "Сотрудникам" },
  {
    audience: "admin",
    title: "Администраторам",
    note: "Разделы «Управления» видны только администраторам компании.",
  },
];

/** Текст для поиска считаем один раз: статьи не меняются. */
const INDEX = ARTICLES.map((article) => ({
  article,
  text: searchable(`${article.title} ${article.keywords ?? ""} ${textOf(article.body)}`),
}));

function articleFromHash(hash: string): string | null {
  const slug = hash.replace(/^#/, "");
  return ARTICLES.some((article) => article.slug === slug) ? slug : null;
}

function toggleId(slug: string): string {
  return `${slug}-toggle`;
}

/**
 * Помощь (ТЗ §8): статьи для сотрудников и администраторов с поиском и
 * «Написать в поддержку». Доступна всем, кто вошёл, — и без компании.
 * Статья открывается по ссылке /help#slug.
 */
export function HelpPage() {
  useDocumentTitle("Помощь");
  const { hash } = useLocation();
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<ReadonlySet<string>>(() => {
    const slug = articleFromHash(hash);
    return new Set(slug ? [slug] : []);
  });

  // Перешли по ссылке на статью (из другой статьи или снаружи) — открыть
  // её; если поиск её скрывает — сбросить поиск.
  const [shownHash, setShownHash] = useState(hash);
  if (shownHash !== hash) {
    setShownHash(hash);
    const slug = articleFromHash(hash);
    if (slug) {
      setOpen((current) => new Set(current).add(slug));
      if (!INDEX.some((item) => item.article.slug === slug && matches(item.text, query))) {
        setQuery("");
      }
    }
  }

  // …и показать её: прокрутить и поставить фокус на заголовок.
  useEffect(() => {
    const target = hash.replace(/^#/, "");
    const slug = articleFromHash(hash);
    const focus = slug
      ? document.getElementById(toggleId(slug))
      : target === SUPPORT
        ? document.getElementById(`${SUPPORT}-title`)
        : null;
    if (!focus) return;
    document.getElementById(slug ?? SUPPORT)?.scrollIntoView({ block: "start" });
    focus.focus({ preventScroll: true });
  }, [hash]);

  function toggle(slug: string) {
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(slug)) next.delete(slug);
      else next.add(slug);
      return next;
    });
  }

  const found = INDEX.filter((item) => matches(item.text, query)).map((item) => item.article);
  const searching = query.trim() !== "";

  return (
    <Page>
      <PageHeader
        title="Помощь"
        description="Короткие ответы о том, как устроен kronto. Не нашли нужного — напишите нам."
        actions={
          <Link to={`/help#${SUPPORT}`} className={buttonClass("ghost", "sm")}>
            Написать в поддержку
          </Link>
        }
      />
      <div className={styles.container}>
        <div className={styles.layout}>
          <div className={styles.main}>
            <div role="search" className={styles.search}>
              <Search size={18} aria-hidden className={styles.searchIcon} />
              <input
                type="search"
                className={styles.searchInput}
                aria-label="Поиск по статьям"
                placeholder="Например: пароль, файл, приглашение"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>
            <p className="visually-hidden" aria-live="polite">
              {searching
                ? `Найдено ${found.length} ${plural(found.length, "статья", "статьи", "статей")}`
                : ""}
            </p>
            {found.length === 0 ? (
              <EmptyState icon={<SearchX size={32} aria-hidden />} title="Ничего не нашлось">
                <p>
                  Попробуйте другие слова или{" "}
                  <Link to={`/help#${SUPPORT}`}>напишите в поддержку</Link>.
                </p>
              </EmptyState>
            ) : (
              GROUPS.map((group) => {
                const items = found.filter((article) => article.audience === group.audience);
                return items.length ? (
                  <ArticleGroup
                    key={group.audience}
                    title={group.title}
                    note={group.note}
                    items={items}
                    open={open}
                    onToggle={toggle}
                  />
                ) : null;
              })
            )}
          </div>
          <SupportSection id={SUPPORT} />
        </div>
      </div>
    </Page>
  );
}

function ArticleGroup({
  title,
  note,
  items,
  open,
  onToggle,
}: {
  title: string;
  note?: string;
  items: Article[];
  open: ReadonlySet<string>;
  onToggle: (slug: string) => void;
}) {
  const titleId = useId();
  return (
    <section className={styles.group} aria-labelledby={titleId}>
      <div className={styles.groupHead}>
        <h2 id={titleId} className={styles.groupTitle}>
          {title}
        </h2>
        {note ? <p className={styles.groupNote}>{note}</p> : null}
      </div>
      <ul className={styles.articles}>
        {items.map((article) => (
          <ArticleItem
            key={article.slug}
            article={article}
            open={open.has(article.slug)}
            onToggle={() => onToggle(article.slug)}
          />
        ))}
      </ul>
    </section>
  );
}

function ArticleItem({
  article,
  open,
  onToggle,
}: {
  article: Article;
  open: boolean;
  onToggle: () => void;
}) {
  const bodyId = `${article.slug}-body`;
  return (
    <li id={article.slug} className={styles.article}>
      <h3 className={styles.articleTitle}>
        <button
          type="button"
          id={toggleId(article.slug)}
          className={styles.toggle}
          aria-expanded={open}
          aria-controls={bodyId}
          onClick={onToggle}
        >
          <span>{article.title}</span>
          <ChevronDown size={18} aria-hidden className={styles.chevron} />
        </button>
      </h3>
      <div id={bodyId} className={styles.body} hidden={!open}>
        {article.body}
        <CopyLink article={article} />
      </div>
    </li>
  );
}

/** Ссылка на статью — чтобы отправить коллеге. */
function CopyLink({ article }: { article: Article }) {
  const toast = useToast();
  async function copy() {
    try {
      await navigator.clipboard.writeText(`${window.location.origin}/help#${article.slug}`);
      toast.show("Ссылка на статью скопирована");
    } catch {
      toast.show("Не удалось скопировать: браузер не дал доступа к буферу обмена", {
        tone: "error",
      });
    }
  }
  return (
    <div>
      <Button variant="link" size="xs" className={styles.copy} onClick={() => void copy()}>
        <Link2 size={14} aria-hidden /> Скопировать ссылку
        <span className="visually-hidden"> на статью «{article.title}»</span>
      </Button>
    </div>
  );
}
