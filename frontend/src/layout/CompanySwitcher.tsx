import { Building2, ChevronsUpDown, Plus } from "lucide-react";
import { Link } from "react-router";

import { companyInitials } from "../lib/initials";
import { Avatar } from "../ui/Avatar";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "../ui/DropdownMenu";
import { Tooltip } from "../ui/Tooltip";
import styles from "./Sidebar.module.css";

export interface CompanyOption {
  id: string;
  name: string;
  /** Подпись под названием: роль или «ждёт одобрения». */
  caption?: string;
  /** Перейти нельзя: заявка ждёт одобрения или доступ закрыт. */
  disabled?: boolean;
}

/**
 * Переключатель компании в шапке боковой панели (ТЗ §1–2): все компании
 * человека, переход — новая пара токенов (/auth/switch-company). Внизу —
 * вступить в ещё одну по приглашению.
 */
export function CompanySwitcher({
  companies,
  currentId,
  onSelect,
  collapsed = false,
}: {
  companies: CompanyOption[];
  currentId: string | null;
  onSelect?: (id: string) => void;
  collapsed?: boolean;
}) {
  const current = companies.find((company) => company.id === currentId) ?? null;
  const name = current?.name ?? "Без компании";
  const caption = current ? current.caption : "вступите по приглашению";
  return (
    <DropdownMenu>
      <Tooltip content={name} side="right" disabled={!collapsed}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className={styles.switcher}
            aria-label={`Компания: ${name}${caption ? `, ${caption}` : ""}`}
          >
            {current ? (
              <Avatar
                shape="square"
                colorful
                name={current.name}
                initials={companyInitials(current.name)}
              />
            ) : (
              <Avatar shape="square" name={name} initials={<Building2 size={16} aria-hidden />} />
            )}
            <span className={styles.switcherText}>
              <span className={styles.switcherName}>{name}</span>
              {caption ? <span className={`mono ${styles.switcherCaption}`}>{caption}</span> : null}
            </span>
            <ChevronsUpDown size={16} aria-hidden className={styles.chevron} />
          </button>
        </DropdownMenuTrigger>
      </Tooltip>
      <DropdownMenuContent
        align="start"
        side={collapsed ? "right" : "bottom"}
        className={styles.switcherMenu}
      >
        {companies.length ? (
          <>
            <DropdownMenuLabel>
              {companies.length > 1 ? "Ваши компании" : "Ваша компания"}
            </DropdownMenuLabel>
            <DropdownMenuRadioGroup
              value={current?.id ?? ""}
              onValueChange={(id: string) => {
                if (id !== current?.id) onSelect?.(id);
              }}
            >
              {companies.map((company) => (
                <DropdownMenuRadioItem
                  key={company.id}
                  value={company.id}
                  disabled={company.disabled}
                  icon={
                    <Avatar
                      shape="square"
                      size="sm"
                      colorful
                      name={company.name}
                      initials={companyInitials(company.name)}
                    />
                  }
                >
                  <span className={styles.optionName}>{company.name}</span>
                  {company.caption ? (
                    <span className={`mono ${styles.optionCaption}`}>{company.caption}</span>
                  ) : null}
                </DropdownMenuRadioItem>
              ))}
            </DropdownMenuRadioGroup>
            <DropdownMenuSeparator />
          </>
        ) : null}
        <DropdownMenuItem asChild>
          <Link to="/settings/companies">
            <Plus size={16} aria-hidden /> Вступить в компанию
          </Link>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
