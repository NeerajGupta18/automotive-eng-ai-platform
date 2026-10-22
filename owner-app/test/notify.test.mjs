// Phase D: the Android app's own schedule and the Web Push key conversion (owner-app/src/lib/notify.js).
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { deviceLabel, plan, urlBase64ToUint8Array } from '../src/lib/notify.js'

const at = (d) => new Date(Date.UTC(2026, 9, d, 3, 30)).toISOString() // 09:00 IST

test('plan keeps future notifications, soonest first, at most max', () => {
  const now = Date.UTC(2026, 9, 22, 4, 0)
  const items = [
    { id: 3, at: at(30), title: 'Insurance expires tomorrow', body: 'b', path: '/cars/1?tab=documents' },
    { id: 1, at: at(22), title: 'past (09:00 today already gone)', body: '', path: '/' },
    { id: 2, at: at(25), title: 'Periodic service due in 7 days', body: 'b', path: '/cars/1?tab=reminders' },
    { id: 'x', at: at(26), title: 'bad id', body: '', path: '/' },
  ]
  const p = plan(items, now)
  assert.deepEqual(p.map((n) => n.id), [2, 3])
  assert.equal(p[0].schedule.at.toISOString(), at(25))
  assert.equal(p[0].schedule.allowWhileIdle, true)
  assert.deepEqual(p[0].extra, { path: '/cars/1?tab=reminders' })
  assert.equal(plan(items, now, 1).length, 1)
  assert.deepEqual(plan(undefined, now), [])
})

test('VAPID public key turns into the 65 bytes the browser expects', () => {
  const key = 'BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4'
  const bytes = urlBase64ToUint8Array(key)
  assert.equal(bytes.length, 65)
  assert.equal(bytes[0], 4)
  assert.equal(Buffer.from(bytes).toString('base64url'), key)
})

test('device labels people recognise', () => {
  assert.equal(deviceLabel('Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/129.0 Mobile Safari/537.36'), 'Chrome on Android')
  assert.equal(deviceLabel('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/129.0 Safari/537.36 Edg/129.0'), 'Edge on Windows')
  assert.equal(deviceLabel('Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) Gecko/20100101 Firefox/131.0'), 'Firefox on Mac')
  assert.equal(deviceLabel('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1'), 'Safari on iPhone')
})
