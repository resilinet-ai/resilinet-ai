// Themes (decision D10): Midnight by default; Clarity, Evergreen and High contrast
// selectable. The choice is a per-browser convenience, so it lives in localStorage and
// the UI works the same when storage is unavailable.

export const THEMES = [
  { id: "midnight", label: "Midnight" },
  { id: "clarity", label: "Clarity" },
  { id: "evergreen", label: "Evergreen" },
  { id: "contrast", label: "High contrast" },
] as const;

export type ThemeId = (typeof THEMES)[number]["id"];

const KEY = "rai-theme";
const DEFAULT: ThemeId = "midnight";

function isTheme(value: unknown): value is ThemeId {
  return THEMES.some((t) => t.id === value);
}

export function savedTheme(): ThemeId {
  try {
    const value = window.localStorage.getItem(KEY);
    return isTheme(value) ? value : DEFAULT;
  } catch {
    return DEFAULT;
  }
}

export function applyTheme(theme: ThemeId): void {
  document.documentElement.setAttribute("data-theme", theme);
  try {
    window.localStorage.setItem(KEY, theme);
  } catch {
    // storage blocked: the theme still applies for this page
  }
}
