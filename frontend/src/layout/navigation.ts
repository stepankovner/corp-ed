import {
  BookA,
  ChartColumn,
  FileText,
  Network,
  Plug,
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
  { to: "/admin/documents", label: "Документы", icon: FileText },
  { to: "/admin/connectors", label: "Подключения", icon: Plug },
  { to: "/admin/users", label: "Сотрудники", icon: Users },
  { to: "/admin/departments", label: "Отделы", icon: Network },
  { to: "/admin/gaps", label: "Пробелы в документах", icon: SearchX },
  { to: "/admin/glossary", label: "Глоссарий", icon: BookA },
  { to: "/admin/usage", label: "Лимит вопросов", icon: ChartColumn },
  { to: "/admin/audit", label: "Журнал действий", icon: ScrollText },
];
