import { Monitor, Moon, Sun } from "lucide-react";

import { THEME_OPTIONS, useTheme } from "../lib/theme";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "../ui/DropdownMenu";
import { IconButton } from "../ui/IconButton";

const ICONS = { system: Monitor, light: Sun, dark: Moon };

/** Выбор темы пунктами-переключателями внутри выпадающего меню. */
export function ThemeOptions() {
  const { preference, setPreference } = useTheme();
  return (
    <>
      <DropdownMenuLabel>Тема</DropdownMenuLabel>
      <DropdownMenuRadioGroup
        aria-label="Тема"
        value={preference}
        onValueChange={(value) => {
          const option = THEME_OPTIONS.find((item) => item.value === value);
          if (option) setPreference(option.value);
        }}
      >
        {THEME_OPTIONS.map(({ value, label }) => {
          const Icon = ICONS[value];
          return (
            <DropdownMenuRadioItem
              key={value}
              value={value}
              icon={<Icon size={16} aria-hidden />}
              // Меню не закрываем: видно, как сменилась тема.
              onSelect={(event) => event.preventDefault()}
            >
              {label}
            </DropdownMenuRadioItem>
          );
        })}
      </DropdownMenuRadioGroup>
    </>
  );
}

/** Кнопка темы для страниц без боковой панели: вход, приглашение, тарифы. */
export function ThemeMenu() {
  const { theme } = useTheme();
  const Icon = ICONS[theme];
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <IconButton label="Тема оформления">
          <Icon size={18} aria-hidden />
        </IconButton>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <ThemeOptions />
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
