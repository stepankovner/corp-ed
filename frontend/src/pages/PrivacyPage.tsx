import { Link } from "react-router";

import { useDocumentTitle } from "../lib/title";
import authStyles from "./AuthLayout.module.css";
import { AuthLayout } from "./AuthLayout";

/**
 * Заглушка политики обработки персональных данных (ТЗ §11): текст
 * готовит ИП. Пока его нет, на боевом домене регистрация только по
 * приглашению (REGISTRATION_ENABLED=false).
 */
export function PrivacyPage() {
  useDocumentTitle("Политика обработки персональных данных");
  return (
    <AuthLayout bar="kronto" title="Политика обработки персональных данных">
      <div className={authStyles.form}>
        <p>
          Текст политики готовится и появится здесь до открытия регистрации для всех. Мы
          обрабатываем только данные, нужные для работы сервиса: имя, фамилию, почту, данные входа и
          вопросы, которые вы задаёте.
        </p>
        <p className="muted">Вопросы о персональных данных — в поддержку kronto.</p>
        <Link to="/">На главную</Link>
      </div>
    </AuthLayout>
  );
}
