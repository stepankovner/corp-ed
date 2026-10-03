import {
  BookUser,
  ChevronDown,
  House,
  PanelLeftClose,
  PanelLeftOpen,
  Settings2,
  SquarePen,
  X,
  type LucideIcon,
} from "lucide-react";
import { useId, useState, type Ref } from "react";
import { Link, matchPath, useLocation, useNavigate } from "react-router";

import { errorMessage } from "../api/errors";
import { isAdmin, needsStrongFactor, useAuth, useMe, type Me } from "../auth/context";
import { IconButton } from "../ui/IconButton";
import { Logo } from "../ui/Logo";
import { Tooltip } from "../ui/Tooltip";
import { useToast } from "../ui/useToast";
import { AccountMenu } from "./AccountMenu";
import { CompanySwitcher, type CompanyOption } from "./CompanySwitcher";
import { ConversationList } from "./ConversationList";
import { ADMIN_SECTIONS } from "./navigation";
import styles from "./Sidebar.module.css";

export type SidebarMode = "expanded" | "collapsed" | "drawer";

interface Props {
  mode: SidebarMode;
  /** Свернуть или развернуть панель (компьютер). */
  onToggle: () => void;
  /** Закрыть выдвижное меню (телефон). */
  onClose: () => void;
  closeRef?: Ref<HTMLButtonElement>;
}

/**
 * Боковая панель приложения, как у Claude и ChatGPT: компания, новый
 * диалог, разделы, учётная запись. На компьютере сворачивается до значков,
 * на телефоне — выдвижное меню (AppShell).
 */
export function Sidebar({ mode, onToggle, onClose, closeRef }: Props) {
  const me = useMe();
  const { switchCompany } = useAuth();
  const toast = useToast();
  const navigate = useNavigate();
  const collapsed = mode === "collapsed";
  // Разделы компании — когда она выбрана и защита входа в порядке.
  const inCompany = me.company !== null && !needsStrongFactor(me);

  async function select(tenantId: string) {
    try {
      await switchCompany(tenantId);
      void navigate("/");
    } catch (err) {
      toast.show(errorMessage(err), { tone: "error" });
    }
  }

  return (
    <div className={`${styles.inner} ${collapsed ? styles.collapsed : ""}`}>
      <div className={styles.head}>
        {collapsed ? null : (
          <Link to="/" className={styles.brand} aria-label="kronto — к вопросам">
            <Logo height={20} />
          </Link>
        )}
        {mode === "drawer" ? (
          <IconButton ref={closeRef} label="Закрыть меню" onClick={onClose} tooltip={false}>
            <X size={20} aria-hidden />
          </IconButton>
        ) : (
          <IconButton
            label={collapsed ? "Развернуть панель" : "Свернуть панель"}
            tooltipSide="right"
            aria-expanded={!collapsed}
            aria-controls="app-sidebar"
            onClick={onToggle}
          >
            {collapsed ? (
              <PanelLeftOpen size={20} aria-hidden />
            ) : (
              <PanelLeftClose size={20} aria-hidden />
            )}
          </IconButton>
        )}
      </div>

      <div className={styles.company}>
        <CompanySwitcher
          companies={companyOptions(me)}
          currentId={me.company?.tenant_id ?? null}
          onSelect={(id) => void select(id)}
          collapsed={collapsed}
        />
      </div>

      {inCompany ? <NewDialogButton collapsed={collapsed} /> : null}

      <div className={styles.scroll}>
        <nav aria-label="Разделы">
          <ul className={styles.list}>
            {inCompany ? (
              <>
                <NavItem to="/people" icon={BookUser} label="Коллеги" collapsed={collapsed} />
                {isAdmin(me) ? <AdminNav collapsed={collapsed} /> : null}
              </>
            ) : (
              <NavItem to="/" end icon={House} label="Главная" collapsed={collapsed} />
            )}
          </ul>
        </nav>
        {/* Диалоги с сервера (ТЗ §6); в свёрнутой панели — только значки разделов. */}
        {inCompany && !collapsed ? <ConversationList /> : null}
      </div>

      <div className={styles.foot}>
        <AccountMenu collapsed={collapsed} />
      </div>
    </div>
  );
}

const ROLE_CAPTION = { admin: "администратор", employee: "сотрудник" } as const;

/** Компании человека для переключателя: ушедшие не показываем. */
function companyOptions(me: Me): CompanyOption[] {
  return me.companies
    .filter((item) => item.status !== "left")
    .map((item) => ({
      id: item.tenant_id,
      name: item.company_name,
      caption:
        item.status === "pending"
          ? "ждёт одобрения"
          : item.status === "blocked"
            ? "доступ закрыт"
            : ROLE_CAPTION[item.role],
      disabled: item.status !== "active",
    }));
}

/** «Новый диалог»: пустой экран вопросов, прежние диалоги — в списке. */
export function NewDialogButton({
  collapsed = false,
  compact = false,
}: {
  collapsed?: boolean;
  /** Кнопка-иконка для верхней полосы телефона. */
  compact?: boolean;
}) {
  const navigate = useNavigate();
  const { pathname } = useLocation();

  function start() {
    if (pathname === "/") document.getElementById("question")?.focus();
    else void navigate("/");
  }

  if (compact) {
    return (
      <IconButton label="Новый диалог" onClick={start} tooltip={false}>
        <SquarePen size={20} aria-hidden />
      </IconButton>
    );
  }
  return (
    <Tooltip content="Новый диалог" side="right" disabled={!collapsed}>
      <button type="button" className={styles.newDialog} onClick={start}>
        <SquarePen size={18} aria-hidden />
        <span className={styles.label}>Новый диалог</span>
      </button>
    </Tooltip>
  );
}

function NavItem({
  to,
  end = false,
  icon: Icon,
  label,
  collapsed,
  nested = false,
}: {
  to: string;
  end?: boolean;
  icon: LucideIcon;
  label: string;
  collapsed: boolean;
  nested?: boolean;
}) {
  // Не NavLink: подсказка (Radix Slot) склеивает className строкой и сломала
  // бы его className-функцию — активность считаем сами.
  const { pathname } = useLocation();
  const active = matchPath({ path: to, end }, pathname) !== null;
  return (
    <li>
      <Tooltip content={label} side="right" disabled={!collapsed}>
        <Link
          to={to}
          aria-current={active ? "page" : undefined}
          className={[styles.item, nested ? styles.nested : "", active ? styles.active : ""].join(
            " ",
          )}
        >
          <Icon size={18} aria-hidden />
          <span className={styles.label}>{label}</span>
        </Link>
      </Tooltip>
    </li>
  );
}

/**
 * «Управление» ведёт к документам и раскрывает разделы. Раскрыто, пока
 * открыт любой раздел управления; стрелка сворачивает и разворачивает вручную.
 */
function AdminNav({ collapsed }: { collapsed: boolean }) {
  const { pathname } = useLocation();
  const listId = useId();
  const inAdmin = pathname.startsWith("/admin");
  const [toggled, setToggled] = useState<boolean | null>(null);
  // Перешли в управление или вышли из него — снова по адресу.
  const [wasInAdmin, setWasInAdmin] = useState(inAdmin);
  if (wasInAdmin !== inAdmin) {
    setWasInAdmin(inAdmin);
    setToggled(null);
  }
  const open = toggled ?? inAdmin;

  return (
    <li>
      <div className={styles.parent}>
        <Tooltip content="Управление" side="right" disabled={!collapsed}>
          <Link
            to="/admin"
            className={[
              styles.item,
              inAdmin ? (open ? styles.parentActive : styles.active) : "",
            ].join(" ")}
          >
            <Settings2 size={18} aria-hidden />
            <span className={styles.label}>Управление</span>
          </Link>
        </Tooltip>
        {collapsed ? null : (
          <IconButton
            size="sm"
            label={open ? "Свернуть разделы управления" : "Показать разделы управления"}
            tooltip={false}
            className={`${styles.disclosure} ${open ? styles.disclosureOpen : ""}`}
            aria-expanded={open}
            aria-controls={open ? listId : undefined}
            onClick={() => setToggled(!open)}
          >
            <ChevronDown size={16} aria-hidden />
          </IconButton>
        )}
      </div>
      {open ? (
        <ul className={styles.subList} id={listId} aria-label="Управление">
          {ADMIN_SECTIONS.map((section) => (
            <NavItem
              key={section.to}
              to={section.to}
              icon={section.icon}
              label={section.label}
              collapsed={collapsed}
              nested
            />
          ))}
        </ul>
      ) : null}
    </li>
  );
}
