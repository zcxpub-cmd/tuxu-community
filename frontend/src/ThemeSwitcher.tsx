import { t } from './i18n'
import { useId } from 'react'
import type { ThemeAppearance, ThemePreference } from './theme'

const choices: { value: ThemePreference; label: string }[] = [
  { value: 'light', label: '日间' },
  { value: 'dark', label: '夜间' },
  { value: 'system', label: '跟随系统' },
]

export default function ThemeSwitcher({ preference, resolvedTheme, setPreference, disabled = false }: ThemeAppearance & { disabled?: boolean }) {
  const name = `workspace-theme-${useId()}`
  return <fieldset className="theme-switcher" disabled={disabled} data-resolved-theme={resolvedTheme}>
    <legend>{t("工作台外观")}</legend>
    <div className="theme-options">{choices.map(choice => <label key={choice.value} className="theme-choice" title={choice.value === 'system' ? t("按电脑本地时间：07:00–19:00 日间，其余时间夜间") : undefined}>
      <input type="radio" name={name} value={choice.value} checked={preference === choice.value} onChange={event => { if (event.target.checked) setPreference(choice.value) }} />
      <span>{t(choice.label)}</span>
    </label>)}</div>
  </fieldset>
}
