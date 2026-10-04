import { Link } from "react-router";

import NotFound from "../pages/NotFoundPage.module.css";
import { useDocumentTitle } from "../lib/title";
import { buttonClass } from "../ui/buttonClass";
import { WindowMark } from "../ui/Logo";
import { SiteLayout } from "./SiteLayout";

/** 404 для гостя — в оболочке сайта; вошедший видит 404 приложения. */
export function SiteNotFound() {
  useDocumentTitle("Страница не найдена");
  return (
    <SiteLayout>
      <div className={NotFound.screen}>
        <span className={NotFound.mark}>
          <WindowMark size={32} />
        </span>
        <p className={`mono ${NotFound.code}`}>ошибка 404</p>
        <h1 className={NotFound.title}>Такой страницы нет</h1>
        <p className={NotFound.text}>Возможно, ссылка устарела или в адресе опечатка.</p>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "center" }}>
          <Link className={buttonClass("dark", "sm")} to="/">
            На главную
          </Link>
          <Link className={buttonClass("ghost", "sm")} to="/login">
            Войти
          </Link>
        </div>
      </div>
    </SiteLayout>
  );
}
