import {
  BookA,
  Building2,
  CreditCard,
  FolderOpen,
  LayoutDashboard,
  Lightbulb,
  Network,
  ScrollText,
  SearchX,
  Users,
  type LucideIcon,
} from "lucide-react";

export interface NavSection {
  to: string;
  label: string;
  icon: LucideIcon;
}

/** Разделы управления — в боковой панели под «Управлением», только администратору. */
export const ADMIN_SECTIONS: NavSection[] = [
  { to: "/admin/overview", label: "Обзор", icon: LayoutDashboard },
  { to: "/admin/sources", label: "Источники", icon: FolderOpen },
  { to: "/admin/users", label: "Сотрудники", icon: Users },
  { to: "/admin/departments", label: "Отделы", icon: Network },
  { to: "/admin/gaps", label: "Пробелы в документах", icon: SearchX },
  { to: "/admin/glossary", label: "Глоссарий", icon: BookA },
  { to: "/admin/suggestions", label: "Подсказки", icon: Lightbulb },
  { to: "/admin/tariff", label: "Тариф", icon: CreditCard },
  { to: "/admin/settings", label: "Настройки компании", icon: Building2 },
  { to: "/admin/audit", label: "Журнал действий", icon: ScrollText },
];
