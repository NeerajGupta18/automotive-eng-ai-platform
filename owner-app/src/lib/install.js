// Install prompts: the Android app (Capacitor), the browser's own install prompt (Chrome, Edge), and Safari's manual steps.
let deferred = null
const listeners = new Set()

window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault()
  deferred = e
  listeners.forEach((fn) => fn())
})

export function inApp() {
  return /CarSaathiApp/.test(navigator.userAgent)
}

export function installed() {
  return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true
}

export const isAndroid = () => /Android/i.test(navigator.userAgent)
export const isIOS = () => /iPhone|iPad|iPod/i.test(navigator.userAgent)

export function canPrompt() {
  return !!deferred
}

export function onPromptChange(fn) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export async function promptInstall() {
  if (!deferred) return false
  deferred.prompt()
  const { outcome } = await deferred.userChoice
  deferred = null
  listeners.forEach((fn) => fn())
  return outcome === 'accepted'
}
