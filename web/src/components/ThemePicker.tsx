import { THEMES, type ThemeId } from "../theme";

export function ThemePicker({ theme, onChange }: { theme: ThemeId; onChange: (t: ThemeId) => void }) {
  return (
    <label className="inline-field">
      <span className="dim">Theme</span>
      <select className="btn sm" value={theme} onChange={(e) => onChange(e.target.value as ThemeId)}>
        {THEMES.map((t) => (
          <option key={t.id} value={t.id}>
            {t.label}
          </option>
        ))}
      </select>
    </label>
  );
}
