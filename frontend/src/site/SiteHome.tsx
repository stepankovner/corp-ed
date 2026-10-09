import {
  ArrowRight,
  BookOpenCheck,
  Building2,
  CloudUpload,
  FileSearch,
  KeyRound,
  Layers,
  Link2,
  MapPin,
  MessageCircleQuestion,
  ShieldCheck,
  SearchX,
  UsersRound,
} from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router";

import { formatPrice, TARIFFS } from "../lib/tariffs";
import { buttonClass } from "../ui/buttonClass";
import { WindowMark } from "../ui/Logo";
import { DemoPreview } from "./DemoPreview";
import styles from "./Home.module.css";
import { SITE, useSiteTitle } from "./meta";
import { SectionHead, SiteLayout } from "./SiteLayout";
import site from "./Site.module.css";

const STEPS = [
  {
    title: "Компания подключает источники",
    text: "Загружает файлы или подключает Битрикс24, Яндекс 360 и Confluence. Права доступа переносятся из системы: сотрудник найдёт только то, что ему и так открыто.",
  },
  {
    title: "kronto разбирает документы на фрагменты",
    text: "Каждый документ делится на смысловые куски, по ним строится поисковый индекс. kronto ищет по смыслу, а не только по совпадению слов. Поправили документ — ответы обновились.",
  },
  {
    title: "Сотрудник спрашивает и получает ответ",
    text: "Обычными словами, как коллегу. В ответе — ссылки на документы: каждую можно открыть и проверить.",
  },
];

const DIFFERENCES: { icon: ReactNode; title: string; text: string }[] = [
  {
    icon: <BookOpenCheck size={22} aria-hidden />,
    title: "Отвечает по документам компании",
    text: "Знает ваши регламенты, стандарты и инструкции и отвечает так, как принято у вас, а не «в среднем по рынку».",
  },
  {
    icon: <Link2 size={22} aria-hidden />,
    title: "К ответу — ссылка на источник",
    text: "Видно, какой документ и какой фрагмент легли в основу ответа. Сотрудник открывает его и проверяет.",
  },
  {
    icon: <SearchX size={22} aria-hidden />,
    title: "Не выдумывает",
    text: "Если ответа в документах нет, kronto так и скажет. Такие вопросы собираются в отчёт: видно, каких документов не хватает.",
  },
  {
    icon: <MapPin size={22} aria-hidden />,
    title: "Данные в России",
    text: "Документы хранятся в России, модели работают у российских облачных провайдеров. Рабочие файлы больше не нужно вставлять в публичные чаты.",
  },
  {
    icon: <KeyRound size={22} aria-hidden />,
    title: "Права — как в ваших системах",
    text: "Документ из закрытого раздела видят только те, кому он открыт в Битрикс24, Яндекс 360 или Confluence. Загруженные файлы раскладываются по папкам с доступом по отделам.",
  },
  {
    icon: <UsersRound size={22} aria-hidden />,
    title: "Не нужна своя ML-команда",
    text: "Внедрение бесплатно: источники подключаем вместе с вами на созвоне, сопровождение — на нашей стороне.",
  },
];

const SOURCES = [
  {
    title: "Файлы",
    text: "Word, Excel, PowerPoint, PDF, текст и Markdown — загрузкой в разделе «Источники», с папками и доступом по отделам.",
  },
  {
    title: "Битрикс24",
    text: "Диск и база знаний. Права на документы переносятся из Битрикс24.",
  },
  {
    title: "Яндекс 360",
    text: "Яндекс Диск и Яндекс Вики — в общем пространстве компании и у каждого сотрудника.",
  },
  {
    title: "Confluence Server / Data Center",
    text: "Пространства и страницы с правами доступа. Подключаем по запросу после проверки на вашем тестовом пространстве.",
  },
];

const SECURITY: { icon: ReactNode; text: string }[] = [
  { icon: <MapPin size={20} aria-hidden />, text: "Документы и вопросы хранятся в России" },
  {
    icon: <Layers size={20} aria-hidden />,
    text: "Данные каждой компании изолированы на уровне базы данных",
  },
  {
    icon: <ShieldCheck size={20} aria-hidden />,
    text: "Второй фактор при входе для всех, ключи доступа и приложение — для администраторов",
  },
  {
    icon: <FileSearch size={20} aria-hidden />,
    text: "Администратор видит вопросы сотрудников только обезличенно",
  },
];

const FAQ: { question: string; answer: ReactNode }[] = [
  {
    question: "Чем это отличается от ChatGPT или Алисы?",
    answer:
      "Публичные чаты отвечают по общим знаниям и не видят документов вашей компании, а рабочие файлы, которые сотрудники туда вставляют, уходят в публичный сервис. kronto отвечает по вашим регламентам и инструкциям, к каждому ответу прикладывает ссылку на документ, а данные хранятся в России.",
  },
  {
    question: "Где хранятся наши документы?",
    answer: (
      <>
        В России: на нашем сервере, данные каждой компании изолированы от других. В тарифе
        «Корпоративный» kronto можно установить в контур компании. Подробнее — на странице{" "}
        <Link to="/security">«Безопасность и данные»</Link>.
      </>
    ),
  },
  {
    question: "Что, если ответа в документах нет?",
    answer:
      "kronto скажет, что в подключённых документах ответа нет. Компания выбирает, что делать дальше: честный отказ или общий ответ с заметной пометкой «не из документов компании». Такие вопросы собираются в отчёт о пробелах — видно, каких документов не хватает.",
  },
  {
    question: "Какие модели используются?",
    answer:
      "Языковые модели у российских облачных провайдеров. По вопросу kronto находит ближайшие по смыслу фрагменты документов, и модель отвечает только по ним — этот подход называется RAG, генерация с опорой на найденные документы.",
  },
  {
    question: "Какие системы можно подключить?",
    answer:
      "Файлы Word, Excel, PowerPoint, PDF, Битрикс24, Яндекс Диск и Яндекс Вики, Confluence Server и Data Center. Другую систему подключим в тарифе «Корпоративный».",
  },
  {
    question: "Сколько стоит?",
    answer: (
      <>
        «Базовый» — 990 ₽ и «Расширенный» — 1 490 ₽ за место в месяц, «Корпоративный» — по запросу.
        Внедрение бесплатно. Подробнее — на странице <Link to="/pricing">«Тарифы»</Link>.
      </>
    ),
  },
  {
    question: "Кто видит вопросы сотрудников?",
    answer:
      "Сам сотрудник — свои диалоги. Администратор компании видит частые вопросы и пробелы в документах обезличенно, без имён. Поделиться диалогом с коллегой сотрудник может сам, ссылкой.",
  },
  {
    question: "Сколько документов нужно для старта?",
    answer:
      "Минимума нет — достаточно одного документа в цифровом виде. Чем больше документов подключено, тем чаще kronto находит ответ.",
  },
  {
    question: "Нужна ли своя IT-команда?",
    answer:
      "Нет. Подключение и сопровождение берём на себя. От вашей IT-службы понадобится выдать доступ к системам, где лежат документы.",
  },
];

/** Главная для гостя (ТЗ §1): сайт о продукте. Вошедший видит здесь приложение. */
export function SiteHome() {
  useSiteTitle(SITE.home);
  return (
    <SiteLayout>
      <Hero />

      <section className={site.section} aria-labelledby="problem-title">
        <div className={site.container}>
          <SectionHead
            id="problem-title"
            eyebrow="проблема"
            title="Ответ где-то есть. Найти его — отдельная работа"
            lead="Регламенты, стандарты и инструкции лежат на дисках, в вики и на портале. Сотрудник тратит время на поиск, а когда не находит — идёт к коллегам."
          />
          <div className={site.grid}>
            <article className={site.card}>
              <h3 className={site.cardTitle}>Информация есть, но в ней не разобраться</h3>
              <p className={site.cardText}>
                Документы без структуры, одно и то же лежит в трёх местах. Это kronto решает:
                находит нужный фрагмент и отвечает по нему.
              </p>
            </article>
            <article className={site.card}>
              <h3 className={site.cardTitle}>Информации нет вообще</h3>
              <p className={site.cardText}>
                Поиском это не исправить. kronto прямо скажет, что ответа в документах нет, а
                администратор увидит такие вопросы в отчёте о пробелах.
              </p>
            </article>
          </div>
        </div>
      </section>

      <section className={site.sectionAlt} aria-labelledby="how-title" id="how">
        <div className={site.container}>
          <SectionHead
            id="how-title"
            eyebrow="как это работает"
            title="Три шага от документов до ответа"
            lead="Модель не отвечает «из головы»: она получает только фрагменты документов, относящиеся к вопросу, и строит ответ по ним."
          />
          <ol className={site.grid}>
            {STEPS.map((step, index) => (
              <li key={step.title} className={site.card}>
                <span className={site.step}>шаг {index + 1}</span>
                <h3 className={site.cardTitle}>{step.title}</h3>
                <p className={site.cardText}>{step.text}</p>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section className={site.section} aria-labelledby="demo-title" id="demo">
        <div className={site.container}>
          <SectionHead
            id="demo-title"
            eyebrow="демо"
            title="Спросите строительную компанию"
            lead="ООО «Меридиан Строй» — вымышленная компания на 350 сотрудников. Выберите вопрос и откройте источник ответа, а свой вопрос задайте в песочнице — там отвечает настоящий kronto."
          />
          <DemoPreview />
        </div>
      </section>

      <section className={site.sectionAlt} aria-labelledby="diff-title">
        <div className={site.container}>
          <SectionHead
            id="diff-title"
            eyebrow="отличия"
            title="Чем kronto отличается от публичного ИИ-чата"
            lead="kronto умеет то же, что обычная языковая модель: ответить, объяснить, сделать сводку. Разница — он опирается на документы вашей компании и показывает, откуда взял ответ."
          />
          <ul className={site.grid}>
            {DIFFERENCES.map((item) => (
              <li key={item.title} className={site.card}>
                <span className={site.cardIcon}>{item.icon}</span>
                <h3 className={site.cardTitle}>{item.title}</h3>
                <p className={site.cardText}>{item.text}</p>
              </li>
            ))}
          </ul>
        </div>
      </section>

      <section className={site.section} aria-labelledby="sources-title">
        <div className={site.container}>
          <SectionHead
            id="sources-title"
            eyebrow="источники"
            title="Откуда kronto берёт документы"
            lead="Загрузите файлы или подключите системы, где документы уже лежат: kronto сам забирает изменения по расписанию."
          />
          <ul className={styles.sources}>
            {SOURCES.map((source) => (
              <li key={source.title} className={styles.sourceItem}>
                <CloudUpload size={20} aria-hidden className={styles.sourceIcon} />
                <div>
                  <h3 className={styles.sourceTitle}>{source.title}</h3>
                  <p className="muted">{source.text}</p>
                </div>
              </li>
            ))}
          </ul>
        </div>
      </section>

      <section className={site.sectionAlt} aria-labelledby="security-title">
        <div className={`${site.container} ${styles.split}`}>
          <SectionHead
            id="security-title"
            eyebrow="безопасность"
            title="Документы компании остаются документами компании"
            lead="Где хранятся данные, кто что видит и как защищён вход — подробно на отдельной странице."
          />
          <div className={styles.securityList}>
            <ul>
              {SECURITY.map((item) => (
                <li key={item.text}>
                  <span className={styles.securityIcon}>{item.icon}</span>
                  {item.text}
                </li>
              ))}
            </ul>
            <Link to="/security" className={buttonClass("ghost", "sm")}>
              Безопасность и данные <ArrowRight size={16} aria-hidden />
            </Link>
          </div>
        </div>
      </section>

      <section className={site.section} aria-labelledby="pricing-title">
        <div className={site.container}>
          <SectionHead
            id="pricing-title"
            eyebrow="тарифы"
            title="Цена — за рабочее место"
            lead="Тариф выбирает компания целиком, внедрение бесплатно."
          />
          <ul className={styles.plans}>
            {TARIFFS.map((tariff) => (
              <li key={tariff.code} className={styles.plan}>
                <h3 className={styles.planName}>{tariff.name}</h3>
                <p className={styles.planPrice}>
                  {tariff.price === null ? "По запросу" : formatPrice(tariff.price)}
                  {tariff.price === null ? null : <span className="muted"> за место в месяц</span>}
                </p>
                <p className="muted">{tariff.summary}</p>
              </li>
            ))}
          </ul>
          <p className={styles.plansMore}>
            <Link to="/pricing" className={buttonClass("ghost", "sm")}>
              Что входит в тарифы <ArrowRight size={16} aria-hidden />
            </Link>
          </p>
        </div>
      </section>

      <section className={site.sectionAlt} aria-labelledby="faq-title">
        <div className={site.container}>
          <SectionHead id="faq-title" eyebrow="вопросы" title="Вопросы и ответы" />
          <div className={styles.faq}>
            {FAQ.map((item) => (
              <details key={item.question} className={styles.faqItem}>
                <summary>
                  <MessageCircleQuestion size={18} aria-hidden className={styles.faqIcon} />
                  {item.question}
                </summary>
                <p>{item.answer}</p>
              </details>
            ))}
          </div>
        </div>
      </section>

      <section className={site.section} aria-labelledby="cta-title">
        <div className={site.container}>
          <div className={site.cta}>
            <Building2 size={28} aria-hidden />
            <h2 id="cta-title">Покажем kronto на ваших документах</h2>
            <p>
              Выберите удобное время — мы перезвоним, подключим источники вместе с вами и покажем
              ассистента на ваших задачах.
            </p>
            <div className={site.actions}>
              <Link to="/pricing/request" className={buttonClass("dark")}>
                Записаться на созвон
              </Link>
              <Link to="/demo" className={buttonClass("ghost")}>
                Попробовать в песочнице
              </Link>
            </div>
          </div>
        </div>
      </section>
    </SiteLayout>
  );
}

function Hero() {
  return (
    <section className={styles.hero} aria-labelledby="hero-title">
      <div className={`${site.container} ${styles.heroGrid}`}>
        <div className={styles.heroText}>
          <p className={`mono ${site.eyebrow}`}>ии-ассистент по документам компании</p>
          <h1 id="hero-title" className={styles.heroTitle}>
            Спросите — и получите ответ по документам компании со ссылкой на источник
          </h1>
          <p className={site.lead}>
            kronto — ассистент для всех сотрудников компании. В отличие от публичного чата, он
            отвечает по вашим регламентам и инструкциям, а данные хранятся в России.
          </p>
          <div className={site.actions}>
            <Link to="/demo" className={buttonClass("dark")}>
              Попробовать в песочнице
            </Link>
            <Link to="/pricing/request" className={buttonClass("ghost")}>
              Записаться на созвон
            </Link>
          </div>
        </div>
        <figure className={styles.heroCard} aria-label="Пример ответа kronto">
          <div className={styles.heroBar}>
            <WindowMark />
            <span>kronto</span>
            <span className={`mono ${styles.heroCompany}`}>ООО «Меридиан Строй»</span>
          </div>
          <div className={styles.heroBody}>
            <p className={styles.heroQuestion}>
              До какой суммы можно купить материалы без тендера?
            </p>
            <p className={styles.heroAnswer}>
              До <strong>300 000 ₽ с НДС</strong> по одной закупке
              <span className={styles.heroCite} aria-label="источник 1">
                1
              </span>
              . Если сумма больше, отдел снабжения запрашивает котировки минимум у трёх поставщиков.
            </p>
            <div className={styles.heroSource}>
              <span className={`mono ${styles.heroSourceNumber}`}>1</span>
              <div>
                <p className={styles.heroSourceTitle}>Положение о закупках</p>
                <p className="mono muted">раздел 3 · п. 3.4</p>
              </div>
            </div>
          </div>
          <figcaption className={`mono ${styles.heroCaption}`}>
            пример на вымышленных документах
          </figcaption>
        </figure>
      </div>
    </section>
  );
}
