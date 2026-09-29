import { Link } from "react-router";

import { buttonClass } from "../ui/buttonClass";
import { Page, EmptyState } from "../ui/Page";

export function NotFoundPage() {
  return (
    <Page>
      <EmptyState title="Такой страницы нет">
        <p>Возможно, ссылка устарела.</p>
        <Link className={buttonClass("dark", "sm")} to="/">
          К вопросам
        </Link>
      </EmptyState>
    </Page>
  );
}
