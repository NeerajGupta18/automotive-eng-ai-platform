// One small client for the owner API: bearer token from storage, readable errors, offline detection.
const KEY = 'cs_token'

export function getToken() {
  try { return localStorage.getItem(KEY) } catch { return null }
}

export function setToken(token) {
  try { token ? localStorage.setItem(KEY, token) : localStorage.removeItem(KEY) } catch { /* private mode: session-only */ }
}

export class ApiError extends Error {
  constructor(status, message, code) {
    super(message)
    this.status = status
    this.code = code
  }
}

function detailOf(data) {
  const d = data && data.detail
  if (Array.isArray(d)) return d.map((x) => x.msg).join('; ')
  return d || 'Something went wrong. Please try again.'
}

export async function api(path, { method = 'GET', body, form, raw } = {}) {
  const headers = {}
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  let payload
  if (form) payload = form
  else if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }
  let res
  try {
    res = await fetch(`/api${path}`, { method, headers, body: payload })
  } catch {
    window.dispatchEvent(new Event('cs-offline'))
    throw new ApiError(0, 'No connection. Check your internet and try again.')
  }
  window.dispatchEvent(new Event('cs-online'))
  if (res.status === 401 && token) {
    setToken(null)
    window.dispatchEvent(new Event('cs-signed-out'))
    throw new ApiError(401, 'Please sign in again.')
  }
  if (raw) {
    if (!res.ok) throw new ApiError(res.status, detailOf(await res.json().catch(() => ({}))))
    return res
  }
  if (res.status === 204) return null
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new ApiError(res.status, detailOf(data), data.code)
  return data
}

export async function download(path, filename) {
  const res = await api(path, { raw: true })
  const url = URL.createObjectURL(await res.blob())
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 10000)
}
