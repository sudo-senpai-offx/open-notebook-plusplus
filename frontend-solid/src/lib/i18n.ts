import i18n from 'i18next'
import { createSignal } from 'solid-js'
import enUS from './locales/en-US'
import bnIN from './locales/bn-IN'
import caES from './locales/ca-ES'
import deDE from './locales/de-DE'
import esES from './locales/es-ES'
import frFR from './locales/fr-FR'
import itIT from './locales/it-IT'
import jaJP from './locales/ja-JP'
import plPL from './locales/pl-PL'
import ptBR from './locales/pt-BR'
import ruRU from './locales/ru-RU'
import trTR from './locales/tr-TR'
import zhCN from './locales/zh-CN'
import zhTW from './locales/zh-TW'

export const LANGUAGES = [
  { code: 'en-US', label: 'English' },
  { code: 'bn-IN', label: 'বাংলা' },
  { code: 'ca-ES', label: 'Català' },
  { code: 'de-DE', label: 'Deutsch' },
  { code: 'es-ES', label: 'Español' },
  { code: 'fr-FR', label: 'Français' },
  { code: 'it-IT', label: 'Italiano' },
  { code: 'ja-JP', label: '日本語' },
  { code: 'pl-PL', label: 'Polski' },
  { code: 'pt-BR', label: 'Português' },
  { code: 'ru-RU', label: 'Русский' },
  { code: 'tr-TR', label: 'Türkçe' },
  { code: 'zh-CN', label: '简体中文' },
  { code: 'zh-TW', label: '繁體中文' },
] as const

const resources: Record<string, { translation: unknown }> = {
  'en-US': { translation: enUS },
  'bn-IN': { translation: bnIN },
  'ca-ES': { translation: caES },
  'de-DE': { translation: deDE },
  'es-ES': { translation: esES },
  'fr-FR': { translation: frFR },
  'it-IT': { translation: itIT },
  'ja-JP': { translation: jaJP },
  'pl-PL': { translation: plPL },
  'pt-BR': { translation: ptBR },
  'ru-RU': { translation: ruRU },
  'tr-TR': { translation: trTR },
  'zh-CN': { translation: zhCN },
  'zh-TW': { translation: zhTW },
}

i18n.init({
  resources,
  fallbackLng: 'en-US',
  interpolation: {
    escapeValue: false,
  },
})

// Reactive language signal: components that call t() during render subscribe
// to this signal, so the whole UI re-renders when the language changes.
const [language, setLanguageSignal] = createSignal(i18n.language)

export function getLanguage(): string {
  return language()
}

export async function setLanguage(lang: string): Promise<void> {
  await i18n.changeLanguage(lang)
  setLanguageSignal(lang)
}

export function t(
  key: string,
  params?: Record<string, string | number>,
): string {
  // Read the signal so callers react to language changes.
  void language()
  let value = i18n.t(key)
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      value = value.replace(new RegExp(`{{${k}}}`, 'g'), String(v))
    }
  }
  return value as string
}

export default i18n