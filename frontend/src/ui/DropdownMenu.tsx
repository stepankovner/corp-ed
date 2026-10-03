import * as Menu from "@radix-ui/react-dropdown-menu";
import { Check } from "lucide-react";
import type { ComponentProps, ReactNode } from "react";

import styles from "./DropdownMenu.module.css";

/**
 * Выпадающее меню на Radix: клавиатура (стрелки, Home/End, буквы, Esc),
 * фокус и роли menu/menuitem — из коробки. Здесь только вид.
 */
export const DropdownMenu = Menu.Root;
export const DropdownMenuTrigger = Menu.Trigger;
export const DropdownMenuGroup = Menu.Group;

export function DropdownMenuContent({
  className,
  sideOffset = 6,
  children,
  ...rest
}: ComponentProps<typeof Menu.Content>) {
  return (
    <Menu.Portal>
      <Menu.Content
        className={[styles.content, className].filter(Boolean).join(" ")}
        sideOffset={sideOffset}
        collisionPadding={8}
        {...rest}
      >
        {children}
      </Menu.Content>
    </Menu.Portal>
  );
}

/** Пункт меню. С asChild — ссылка (Link): значок кладите внутрь неё. */
export function DropdownMenuItem({
  className,
  icon,
  asChild,
  children,
  ...rest
}: ComponentProps<typeof Menu.Item> & { icon?: ReactNode }) {
  return (
    <Menu.Item
      className={[styles.item, className].filter(Boolean).join(" ")}
      asChild={asChild}
      {...rest}
    >
      {asChild ? (
        children
      ) : (
        <>
          {icon ? <span className={styles.icon}>{icon}</span> : null}
          {children}
        </>
      )}
    </Menu.Item>
  );
}

export function DropdownMenuLabel({ children }: { children: ReactNode }) {
  return <Menu.Label className={`mono ${styles.label}`}>{children}</Menu.Label>;
}

export function DropdownMenuSeparator() {
  return <Menu.Separator className={styles.separator} />;
}

export const DropdownMenuRadioGroup = Menu.RadioGroup;

export function DropdownMenuRadioItem({
  icon,
  children,
  ...rest
}: ComponentProps<typeof Menu.RadioItem> & { icon?: ReactNode }) {
  return (
    <Menu.RadioItem className={styles.item} {...rest}>
      {icon ? <span className={styles.icon}>{icon}</span> : null}
      <span className={styles.text}>{children}</span>
      <Menu.ItemIndicator className={styles.indicator}>
        <Check size={16} aria-hidden />
      </Menu.ItemIndicator>
    </Menu.RadioItem>
  );
}
