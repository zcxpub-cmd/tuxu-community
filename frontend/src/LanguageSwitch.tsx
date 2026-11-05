import { currentLocale, setLocale, t } from './i18n'

export default function LanguageSwitch() {
  const language = currentLocale()
  return <div className="language-switch" role="group" aria-label={t('界面语言')}>
    <button type="button" lang="zh-CN" aria-pressed={language === 'zh'} onClick={() => setLocale('zh')}>中文</button>
    <button type="button" lang="en" aria-pressed={language === 'en'} onClick={() => setLocale('en')}>English</button>
  </div>
}
