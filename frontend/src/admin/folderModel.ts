import { useQuery } from "@tanstack/react-query";

import { api, unwrap, type Schemas } from "../api/client";

export type Folder = Schemas["FolderResponse"];

/** Ключ папок: счётчики документов в них меняют загрузка, перенос и удаление. */
export const FOLDERS_KEY = ["folders"] as const;

/**
 * Что открыто во вкладке «Файлы» — значение ?folder= в адресе: все файлы,
 * общие документы (загруженные без папки), документы из подключений или id папки.
 */
export const ALL = "all";
export const ROOT = "root";
export const CONNECTORS = "connectors";

export const ROOT_NAME = "Общие документы";

export function useFolders() {
  return useQuery({
    queryKey: FOLDERS_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/folders")),
  });
}

/** Кто видит папку — коротко, для плитки: «Кадры, Бухгалтерия и ещё 2». */
export function folderAccess(folder: Folder): string {
  if (!folder.restricted) return "Все сотрудники";
  const names = folder.departments.map((department) => department.name);
  if (names.length === 0) return "Только администраторы";
  if (names.length <= 2) return names.join(", ");
  return `${names.slice(0, 2).join(", ")} и ещё ${names.length - 2}`;
}

/**
 * Кто увидит документы папки (null — общие документы) — полностью, для
 * окон загрузки и переноса: «Документы увидят {audience}.»
 */
export function audience(folder: Folder | null): string {
  if (!folder?.restricted) return "все сотрудники";
  const names = folder.departments.map((department) => department.name);
  return names.length
    ? `только администраторы и отделы: ${names.join(", ")}`
    : "только администраторы";
}
