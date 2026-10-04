import { ChevronsUpDown, Download, LogOut, Settings } from "lucide-react";
import { Link } from "react-router";

import { displayName, useAuth, useMe } from "../auth/context";
import { personInitials } from "../lib/initials";
import { useInstallPrompt } from "../lib/install";
import { Avatar } from "../ui/Avatar";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "../ui/DropdownMenu";
import { Tooltip } from "../ui/Tooltip";
import styles from "./Sidebar.module.css";
import { ThemeOptions } from "./ThemeOptions";

const ROLE_CAPTION = { admin: "администратор", employee: "сотрудник" } as const;

/** Учётная запись внизу боковой панели: настройки, тема, выход. */
export function AccountMenu({ collapsed = false }: { collapsed?: boolean }) {
  const me = useMe();
  const { logout } = useAuth();
  const install = useInstallPrompt();
  const name = displayName(me);
  return (
    <DropdownMenu>
      <Tooltip content={name} side="right" disabled={!collapsed}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className={styles.account}
            aria-label={`Профиль: ${name}${me.full_name ? `, ${me.email}` : ""}`}
          >
            <Avatar
              name={name}
              src={me.avatar_url}
              initials={personInitials(me.full_name, me.email)}
            />
            <span className={styles.accountText}>
              <span className={styles.accountName}>{name}</span>
              {me.full_name ? <span className={styles.accountEmail}>{me.email}</span> : null}
            </span>
            <ChevronsUpDown size={16} aria-hidden className={styles.chevron} />
          </button>
        </DropdownMenuTrigger>
      </Tooltip>
      <DropdownMenuContent
        side={collapsed ? "right" : "top"}
        align={collapsed ? "end" : "start"}
        className={styles.accountMenu}
      >
        <div className={styles.menuHead}>
          <span className={styles.menuName}>{name}</span>
          {me.full_name ? <span className={styles.menuEmail}>{me.email}</span> : null}
          <span className={`mono ${styles.menuMeta}`}>
            {me.company ? `${me.company.name} · ${ROLE_CAPTION[me.company.role]}` : "без компании"}
          </span>
        </div>
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link to="/settings">
            <Settings size={16} aria-hidden /> Настройки
          </Link>
        </DropdownMenuItem>
        {install ? (
          <DropdownMenuItem
            icon={<Download size={16} aria-hidden />}
            onSelect={() => void install()}
          >
            Установить приложение
          </DropdownMenuItem>
        ) : null}
        <DropdownMenuSeparator />
        <ThemeOptions />
        <DropdownMenuSeparator />
        <DropdownMenuItem icon={<LogOut size={16} aria-hidden />} onSelect={() => void logout()}>
          Выйти
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
