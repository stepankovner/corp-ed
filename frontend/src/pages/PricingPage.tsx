import { Link } from "react-router";

import { formatPrice, TARIFFS } from "../lib/tariffs";
import { SITE, useSiteTitle } from "../site/meta";
import { SiteLayout } from "../site/SiteLayout";
import { buttonClass } from "../ui/buttonClass";
import styles from "./PricingPage.module.css";

/**
 * Страница тарифов (решение Артёма 30.09). Цены и тексты — только в
 * lib/tariffs.ts. Без утверждений, которых нет в досье: ни «соответствуем
 * 152-ФЗ», ни «запуск за дни», ни выдуманных клиентов.
 */
export function PricingPage() {
  useSiteTitle(SITE.pricing);
  return (
    <SiteLayout>
      <div className={styles.screen}>
        <div className={styles.main}>
          <h1 className={styles.title}>Тарифы</h1>
          <p className={styles.lead}>
            Ассистент отвечает сотрудникам по документам вашей компании — со ссылкой на источник.
            Цена — за рабочее место: сотрудника, который работает за компьютером.
          </p>

          <div className={styles.plans}>
            {TARIFFS.map((tariff, index) => (
              <section
                key={tariff.code}
                className={styles.plan}
                aria-labelledby={`plan-${tariff.code}`}
              >
                <h2 className={styles.planName} id={`plan-${tariff.code}`}>
                  {tariff.name}
                </h2>
                <p>
                  {tariff.price === null ? (
                    <span className={styles.price}>По запросу</span>
                  ) : (
                    <>
                      <span className={styles.price}>{formatPrice(tariff.price)}</span>{" "}
                      <span className={styles.per}>за место в месяц</span>
                    </>
                  )}
                </p>
                <p className={styles.per}>{tariff.summary}</p>
                <ul className={styles.list}>
                  {tariff.features.map((feature) => (
                    <li key={feature}>{feature}</li>
                  ))}
                </ul>
                <Link
                  to={`/pricing/request?tariff=${tariff.code}`}
                  className={buttonClass(index === 0 ? "dark" : "ghost", "md", true)}
                >
                  {tariff.cta}
                </Link>
              </section>
            ))}
          </div>

          <div className={styles.notes}>
            <p className={styles.note}>
              <strong>Внедрение бесплатно</strong>
              Подключаем компанию и её источники вместе с вами на созвоне.
            </p>
            <p className={styles.note}>
              <strong>Как начать</strong>
              Выберите удобное время — мы перезвоним, подтвердим созвон и покажем ассистента на
              ваших задачах.
            </p>
          </div>
        </div>
      </div>
    </SiteLayout>
  );
}
