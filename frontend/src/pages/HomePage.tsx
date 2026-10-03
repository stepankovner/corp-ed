import { needsStrongFactor, useMe } from "../auth/context";
import { ChatPage } from "../chat/ChatPage";
import { NoCompanyHome } from "./NoCompanyHome";
import { StrongFactorRequired } from "./StrongFactorRequired";

/**
 * Главная после входа (ТЗ §2): в компании — вопросы; без компании —
 * вступить по приглашению или подключить свою; администратору без
 * приложения или ключа — сначала защита входа.
 */
export function HomePage() {
  const me = useMe();
  if (!me.company) return <NoCompanyHome />;
  if (needsStrongFactor(me)) return <StrongFactorRequired />;
  return <ChatPage />;
}
