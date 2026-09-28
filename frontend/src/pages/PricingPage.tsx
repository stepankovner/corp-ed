import { Link } from "react-router";

import { buttonClass } from "../ui/buttonClass";
import { Logo } from "../ui/Logo";
import styles from "./PricingPage.module.css";

/**
 * Страница тарифов (досье 3.3, 10.1, 10.4, 15). Цена базового тарифа
 * открыта; тарифы выше — «по запросу», пока их цены не определены.
 * Без утверждений, которых нет в досье: ни «соответствуем 152-ФЗ», ни
 * «запуск за дни», ни выдуманных клиентов.
 */
export function PricingPage() {
  return (
    <div className={styles.screen}>
      <header className={styles.top}>
        <Link to="/pricing" aria-label="Kronto — тарифы">
          <Logo height={24} />
        </Link>
        <Link to="/login" className={buttonClass("ghost", "sm")}>
          Войти
        </Link>
      </header>
      <main className={styles.main}>
        <h1 className={styles.title}>Тарифы</h1>
        <p className={styles.lead}>
          Ассистент отвечает сотрудникам по документам вашей компании — со ссылкой на источник. Цена
          — за рабочее место: сотрудника, который работает за компьютером.
        </p>

        <div className={styles.plans}>
          <section className={styles.plan} aria-labelledby="plan-base">
            <h2 className={styles.planName} id="plan-base">
              Базовый
            </h2>
            <p>
              <span className={styles.price}>1 490 ₽</span>{" "}
              <span className={styles.per}>за место в месяц</span>
            </p>
            <ul className={styles.list}>
              <li>Ответы по документам компании со ссылкой на источник</li>
              <li>Документы файлами и подключения к рабочим системам компании</li>
              <li>Права доступа — как в подключённых системах</li>
              <li>Отчёт о вопросах, на которые в документах нет ответа</li>
              <li>Управление сотрудниками и источниками</li>
            </ul>
            <Link to="/pricing/request?tariff=base" className={buttonClass("dark", "md", true)}>
              Записаться на созвон
            </Link>
          </section>

          <section className={styles.plan} aria-labelledby="plan-custom">
            <h2 className={styles.planName} id="plan-custom">
              Больше источников и вопросов
            </h2>
            <p>
              <span className={styles.price}>По запросу</span>
            </p>
            <p className={styles.per}>
              Для компаний, которым нужно больше подключённых систем или больший объём вопросов.
              Подберём на созвоне.
            </p>
            <Link to="/pricing/request?tariff=custom" className={buttonClass("ghost", "md", true)}>
              Обсудить на созвоне
            </Link>
          </section>
        </div>

        <div className={styles.notes}>
          <p className={styles.note}>
            <strong>Внедрение бесплатно</strong>
            Подключаем компанию и её источники вместе с вами на созвоне.
          </p>
          <p className={styles.note}>
            <strong>Пилот — месяц за полцены</strong>
            745 ₽ за место в первый месяц, число мест не ограничено.
          </p>
          <p className={styles.note}>
            <strong>Как начать</strong>
            Выберите удобное время — мы перезвоним, подтвердим созвон и покажем ассистента на ваших
            задачах.
          </p>
        </div>
      </main>
    </div>
  );
}
