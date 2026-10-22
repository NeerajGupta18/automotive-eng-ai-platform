// Notifications on this device (Phase D).
//
//   Browsers (Chrome and Edge on Android and desktop, Firefox, Safari once the app is added to the home screen): Web Push.
//     The service worker (public/sw.js) shows what the server sends, even when the app is closed.
//   The Android app: its WebView can't receive Web Push, so it schedules the date-based reminders on the phone itself
//     (Capacitor Local Notifications) from the server's list of the coming weeks, each time the app opens. Booking updates
//     reach it as emails and in the app's inbox.
import { api } from '../api.js'

export function urlBase64ToUint8Array(s) {
  const b64 = (s + '='.repeat((4 - (s.length % 4)) % 4)).replace(/-/g, '+').replace(/_/g, '/')
  const raw = atob(b64)
  return Uint8Array.from(raw, (c) => c.charCodeAt(0))
}

function bytesToUrlBase64(buf) {
  let s = ''
  new Uint8Array(buf).forEach((b) => { s += String.fromCharCode(b) })
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

export function nativeApp() {
  return Boolean(window.Capacitor?.isNativePlatform?.())
}

function localPluginAvailable() {
  const cap = window.Capacitor
  return Boolean(cap?.PluginHeaders?.some((p) => p.name === 'LocalNotifications') || cap?.isPluginAvailable?.('LocalNotifications'))
}

// 'web' | 'native' | 'ios-install' (iPhone: add to home screen first) | 'none'
export function pushSupport() {
  if (nativeApp()) return localPluginAvailable() ? 'native' : 'none'
  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent)
  const standalone = window.matchMedia?.('(display-mode: standalone)').matches || navigator.standalone
  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) return ios && !standalone ? 'ios-install' : 'none'
  return 'web'
}

export function deviceLabel(ua = navigator.userAgent) {
  const browser = /Edg\//.test(ua) ? 'Edge' : /Firefox\//.test(ua) ? 'Firefox' : /Chrome\//.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari' : 'Browser'
  const os = /Android/.test(ua) ? 'Android' : /iPhone|iPad/.test(ua) ? 'iPhone' : /Windows/.test(ua) ? 'Windows' : /Mac OS X/.test(ua) ? 'Mac' : /Linux/.test(ua) ? 'Linux' : ''
  return os ? `${browser} on ${os}` : browser
}

// ---------------------------------------------------------------- Web Push
async function registration() {
  return (await navigator.serviceWorker.getRegistration()) || navigator.serviceWorker.register('/sw.js')
}

export async function webPushState() {
  if (pushSupport() !== 'web') return 'unsupported'
  if (Notification.permission === 'denied') return 'blocked'
  const reg = await navigator.serviceWorker.getRegistration()
  const sub = reg && (await reg.pushManager.getSubscription())
  return sub && Notification.permission === 'granted' ? 'on' : 'off'
}

export async function enableWebPush() {
  const perm = await Notification.requestPermission()
  if (perm !== 'granted') {
    throw new Error(perm === 'denied' ? 'Notifications are blocked for this site. Allow them in the browser\'s site settings, then try again.'
      : 'Notifications were not allowed.')
  }
  const { public_key: key } = await api('/push/key')
  if (!key) throw new Error('Push notifications are not set up on this server yet.')
  const reg = await registration()
  await navigator.serviceWorker.ready
  let sub = await reg.pushManager.getSubscription()
  const current = sub?.options?.applicationServerKey
  if (sub && current && bytesToUrlBase64(current) !== key) { await sub.unsubscribe(); sub = null } // the server's key changed
  if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(key) })
  const j = sub.toJSON()
  await api('/push/subscriptions', { method: 'POST', body: { endpoint: j.endpoint, keys: j.keys, label: deviceLabel() } })
}

export async function disableWebPush() {
  const reg = await navigator.serviceWorker.getRegistration()
  const sub = reg && (await reg.pushManager.getSubscription())
  if (!sub) return
  await api('/push/unsubscribe', { method: 'POST', body: { endpoint: sub.endpoint } }).catch(() => {})
  await sub.unsubscribe()
}

// ---------------------------------------------------------------- the Android app's own schedule
// The server's list -> what the phone schedules: future times only (a minute's margin), soonest first, at most `max`.
export function plan(items, now = Date.now(), max = 50) {
  return (items || [])
    .filter((i) => Number.isInteger(i.id) && Date.parse(i.at) > now + 60_000)
    .sort((a, b) => Date.parse(a.at) - Date.parse(b.at))
    .slice(0, max)
    .map((i) => ({ id: i.id, title: i.title, body: i.body, schedule: { at: new Date(Date.parse(i.at)), allowWhileIdle: true },
      extra: { path: i.path } }))
}

let ln = null
async function plugin() {
  if (!ln) ln = (await import('@capacitor/local-notifications')).LocalNotifications
  return ln
}

export async function nativeState() {
  if (pushSupport() !== 'native') return 'unsupported'
  const p = await (await plugin()).checkPermissions()
  return p.display === 'granted' ? 'on' : p.display === 'denied' ? 'blocked' : 'off'
}

export async function enableNative() {
  const L = await plugin()
  const p = await L.requestPermissions()
  if (p.display !== 'granted') throw new Error('Notifications are off for CarSaathi. Turn them on in the phone\'s settings: Apps, CarSaathi, Notifications.')
  return syncNative(true)
}

let lastSync = 0
let listening = false
// Replace the phone's schedule with the server's current list. Cheap; at most every 10 minutes unless forced.
export async function syncNative(force = false, navigate = null) {
  if (pushSupport() !== 'native') return null
  if (!force && Date.now() - lastSync < 10 * 60_000) return null
  lastSync = Date.now()
  const L = await plugin()
  if (navigate && !listening) {
    listening = true
    L.addListener('localNotificationActionPerformed', (e) => { const path = e.notification?.extra?.path; if (path) navigate(path) })
  }
  if ((await L.checkPermissions()).display !== 'granted') return null
  const { items } = await api('/notifications/schedule')
  const pending = (await L.getPending()).notifications
  if (pending.length) await L.cancel({ notifications: pending.map((n) => ({ id: n.id })) })
  const next = plan(items)
  if (next.length) await L.schedule({ notifications: next })
  return next.length
}
