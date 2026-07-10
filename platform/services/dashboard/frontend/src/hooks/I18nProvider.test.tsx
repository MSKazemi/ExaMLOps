import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { I18nProvider } from './I18nProvider'
import { useI18n } from './i18nContext'
import { LocaleSwitcher } from '@/components/LocaleSwitcher'

function Probe() {
  const { t } = useI18n()
  return <h1>{t('finops.title')}</h1>
}

function renderApp() {
  return render(
    <I18nProvider>
      <LocaleSwitcher />
      <Probe />
    </I18nProvider>,
  )
}

describe('I18nProvider', () => {
  beforeEach(() => {
    localStorage.clear()
    document.documentElement.dir = ''
  })

  it('renders English copy by default', () => {
    renderApp()
    expect(screen.getByRole('heading', { name: 'FinOps & Green-AI' })).toBeInTheDocument()
  })

  it('re-renders all copy when the locale switches (GWT-1) and persists it', () => {
    renderApp()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'it' } })
    expect(screen.getByRole('heading', { name: 'FinOps e Green-AI' })).toBeInTheDocument()
    expect(localStorage.getItem('dashboard.locale')).toBe('it')
  })

  it('sets the document language + direction', () => {
    renderApp()
    expect(document.documentElement.lang).toBe('en')
    expect(document.documentElement.dir).toBe('ltr')
  })
})
