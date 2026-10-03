import { ChevronsUpDown } from "lucide-react";

import { companyInitials } from "../lib/initials";
import { Avatar } from "../ui/Avatar";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "../ui/DropdownMenu";
import { Tooltip } from "../ui/Tooltip";
import styles from "./Sidebar.module.css";

export interface CompanyOption {
  id: string;
  name: string;
  /** Подпись под названием: роль человека в этой компании. */
  caption?: string;
}

/**
 * Переключатель компании в шапке боковой панели (ТЗ §1). Сейчас у человека
 * одна компания; когда членств станет несколько (этап 2), сюда придёт их
 * список и onSelect.
 */
export function CompanySwitcher({
  companies,
  currentId,
  onSelect,
  collapsed = false,
}: {
  companies: CompanyOption[];
  currentId: string;
  onSelect?: (id: string) => void;
  collapsed?: boolean;
}) {
  const current = companies.find((company) => company.id === currentId) ?? companies[0];
  if (!current) return null;
  return (
    <DropdownMenu>
      <Tooltip content={current.name} side="right" disabled={!collapsed}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className={styles.switcher}
            aria-label={`Компания: ${current.name}${current.caption ? `, ${current.caption}` : ""}`}
          >
            <Avatar
              shape="square"
              colorful
              name={current.name}
              initials={companyInitials(current.name)}
            />
            <span className={styles.switcherText}>
              <span className={styles.switcherName}>{current.name}</span>
              {current.caption ? (
                <span className={`mono ${styles.switcherCaption}`}>{current.caption}</span>
              ) : null}
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
        <DropdownMenuLabel>
          {companies.length > 1 ? "Ваши компании" : "Ваша компания"}
        </DropdownMenuLabel>
        <DropdownMenuRadioGroup value={current.id} onValueChange={(id) => onSelect?.(id)}>
          {companies.map((company) => (
            <DropdownMenuRadioItem
              key={company.id}
              value={company.id}
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
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
