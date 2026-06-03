import { createContext, useContext, useEffect, useState } from 'react'

export type Theme = 'day' | 'night' | 'midnight'

const STORAGE_KEY = 'examlops-theme'

function getInitialTheme(): Theme {
  try {
    const stored = localStorage.getItem(STORAGE_KEY) as Theme | null
    if (stored === 'day' || stored === 'night' || stored === 'midnight') return stored
  } catch {
    // localStorage unavailable (SSR / privacy mode)
  }
  return 'night'
}

interface ThemeContextValue {
  theme: Theme
  setTheme: (t: Theme) => void
}

const ThemeContext = createContext<ThemeContextValue>({ theme: 'night', setTheme: () => {} })

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(getInitialTheme)

  const setTheme = (t: Theme) => {
    setThemeState(t)
    try { localStorage.setItem(STORAGE_KEY, t) } catch { /* ignore */ }
  }

  useEffect(() => {
    const html = document.documentElement
    html.removeAttribute('data-theme')
    if (theme !== 'night') html.setAttribute('data-theme', theme)
  }, [theme])

  return (
    <ThemeContext.Provider value={{ theme, setTheme }}>
      {children}
    </ThemeContext.Provider>
  )
}

export function useTheme() {
  return useContext(ThemeContext)
}
