import { Link } from "react-router";

import { buttonClass } from "../ui/buttonClass";
import { CONTACTS, SITE, useSiteTitle } from "./meta";
import { DraftNote, SectionHead, SiteLayout } from "./SiteLayout";
import site from "./Site.module.css";

/**
 * «О компании» (ТЗ §1): команда, контакты, реквизиты ИП. Команду и
 * реквизиты присылает владелец — до этого на их месте пометки «заменить».
 */
export function AboutPage() {
  useSiteTitle(SITE.about);
  return (
    <SiteLayout>
      <article className={site.narrow}>
        <SectionHead
          level={1}
          eyebrow="о компании"
          title="Делаем ассистента, которому можно доверить документы компании"
          lead="kronto отвечает сотрудникам по регламентам, стандартам и инструкциям их компании — со ссылкой на источник и без выдумок. Делаем его для компаний, где ответы на рабочие вопросы уже записаны, но их долго искать."
        />
        <div className={site.prose}>
          <h2>Команда</h2>
          <DraftNote>Имена, роли и фотографии команды пришлёт владелец продукта.</DraftNote>
          <p>
            Небольшая команда: продукт и продажи, разработка, машинное обучение. Подключаем компании
            сами — на созвоне, вместе с их IT-службой.
          </p>

          <h2>Контакты</h2>
          <dl>
            <dt>Почта</dt>
            <dd>
              <a href={`mailto:${CONTACTS.email}`}>{CONTACTS.email}</a>
            </dd>
            <dt>Telegram</dt>
            <dd>
              <a href={`https://t.me/${CONTACTS.telegram}`} rel="noopener noreferrer">
                @{CONTACTS.telegram}
              </a>
            </dd>
            <dt>Поддержка</dt>
            <dd>
              Для тех, кто уже работает в kronto, — «Помощь» → «Написать в поддержку» в приложении.
            </dd>
          </dl>

          <h2>Реквизиты</h2>
          <DraftNote>Реквизиты появятся после регистрации ИП — их пришлёт владелец.</DraftNote>
          <dl>
            <dt>Исполнитель</dt>
            <dd>Индивидуальный предприниматель — заменить</dd>
            <dt>ИНН</dt>
            <dd>заменить</dd>
            <dt>ОГРНИП</dt>
            <dd>заменить</dd>
            <dt>Адрес для писем</dt>
            <dd>заменить</dd>
          </dl>

          <div className={site.actions}>
            <Link to="/pricing/request" className={buttonClass("dark")}>
              Записаться на созвон
            </Link>
            <Link to="/demo" className={buttonClass("ghost")}>
              Попробовать в песочнице
            </Link>
          </div>
        </div>
      </article>
    </SiteLayout>
  );
}
