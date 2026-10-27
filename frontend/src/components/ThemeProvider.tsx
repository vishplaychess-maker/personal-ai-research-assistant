import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";

/**
 * Three-theme system (Matte / Obsidian / Paper).
 *
 * The colour values live in `index.css` as `[data-theme=…]` CSS-variable sets,
 * so every component that already uses semantic tokens restyles itself.
 *
 * `data-theme` alone is not enough: many older components still branch on the
 * legacy `.dark` class (Tailwind `dark:` variants and the `.dark .prose` rules).
 * Matte and Obsidian therefore set BOTH, and Paper clears `.dark`. Without that
 * bridge, switching to Paper would leave half the app on dark-only styles.
 */
export type Theme = "matte" | "obsidian" | "paper";

interface ThemeContextValue {
  theme: Theme;
  setTheme: (theme: Theme) => void;
  /** Legacy affordance: toggles between paper and the last dark theme. */
  toggleTheme: () => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

const STORAGE_KEY = "research-assistant-theme";
const LAST_DARK_KEY = "research-assistant-theme-dark";

export const THEMES: { id: Theme; label: string }[] = [
  { id: "matte", label: "Matte" },
  { id: "obsidian", label: "Obsidian" },
  { id: "paper", label: "Paper" },
];

const DARK_THEMES: Theme[] = ["matte", "obsidian"];

function isTheme(value: unknown): value is Theme {
  return value === "matte" || value === "obsidian" || value === "paper";
}

function getInitialTheme(): Theme {
  if (typeof window === "undefined") return "matte";
  const stored = window.localStorage.getItem(STORAGE_KEY);
  if (isTheme(stored)) return stored;
  // Migrate the two-theme era: light → paper, dark → matte.
  if (stored === "light") return "paper";
  if (stored === "dark") return "matte";
  return "matte";
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(getInitialTheme);

  useEffect(() => {
    const root = document.documentElement;
    const isDark = DARK_THEMES.includes(theme);
    root.setAttribute("data-theme", theme);
    // Legacy bridge: keep `dark:` utilities and `.dark .prose` working.
    root.classList.toggle("dark", isDark);
    root.style.colorScheme = isDark ? "dark" : "light";
    window.localStorage.setItem(STORAGE_KEY, theme);
    if (isDark) window.localStorage.setItem(LAST_DARK_KEY, theme);
  }, [theme]);

  const setTheme = (next: Theme) => setThemeState(next);
  const toggleTheme = () =>
    setThemeState((prev) =>
      prev === "paper"
        ? ((window.localStorage.getItem(LAST_DARK_KEY) as Theme) ?? "matte")
        : "paper"
    );

  return (
    <ThemeContext.Provider value={{ theme, setTheme, toggleTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used within a ThemeProvider");
  return ctx;
}
