import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { api, getToken, setToken } from './api.js'

const Ctx = createContext(null)

export function AuthProvider({ children }) {
  const [state, setState] = useState({ user: null, loading: true, config: null })

  const refresh = useCallback(async () => {
    const config = await api('/config').catch(() => null)
    if (!getToken()) {
      setState({ user: null, loading: false, config })
      return
    }
    try {
      const user = await api('/me')
      setState({ user, loading: false, config })
    } catch (e) {
      if (e.status === 401) setToken(null)
      setState({ user: null, loading: false, config, offline: e.status === 0 })
    }
  }, [])

  useEffect(() => {
    refresh()
    const out = () => setState((s) => ({ ...s, user: null }))
    window.addEventListener('cs-signed-out', out)
    return () => window.removeEventListener('cs-signed-out', out)
  }, [refresh])

  const signIn = (token, user) => {
    setToken(token)
    setState((s) => ({ ...s, user }))
  }
  const signOut = async (everywhere = false) => {
    await api(`/auth/logout${everywhere ? '?everywhere=true' : ''}`, { method: 'POST' }).catch(() => {})
    setToken(null)
    setState((s) => ({ ...s, user: null }))
  }
  const setUser = (user) => setState((s) => ({ ...s, user }))

  return <Ctx.Provider value={{ ...state, refresh, signIn, signOut, setUser }}>{children}</Ctx.Provider>
}

export const useAuth = () => useContext(Ctx)
