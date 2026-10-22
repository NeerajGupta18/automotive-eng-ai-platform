import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { ErrorText } from '../components/Field.jsx'
import { disableWebPush, enableNative, enableWebPush, nativeState, pushSupport, syncNative, webPushState } from '../lib/notify.js'
import { ago } from './Notifications.jsx'

function Toggle({ checked, onChange, label, hint, disabled }) {
  return (
    <label className={`toggle${disabled ? ' off' : ''}`}>
      <span className="grow"><span className="title">{label}</span>{hint && <span className="meta">{hint}</span>}</span>
      <input type="checkbox" role="switch" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <i aria-hidden="true" />
    </label>
  )
}

// This device: Web Push in a browser, the phone's own notifications in the Android app.
function ThisDevice({ prefs, reload }) {
  const support = pushSupport()
  const [state, setState] = useState('…')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const check = () => (support === 'native' ? nativeState() : webPushState()).then(setState).catch(() => setState('off'))
  useEffect(() => { check() }, [])

  async function turnOn() {
    setBusy(true); setError('')
    try {
      if (support === 'native') await enableNative()
      else await enableWebPush()
      if (!prefs.push) await api('/notifications/prefs', { method: 'PATCH', body: { push: true } })
      await check(); reload()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }
  async function turnOff() {
    setBusy(true); setError('')
    try { await disableWebPush(); await check(); reload() } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  if (support === 'ios-install') {
    return <p className="small">On iPhone, notifications work once CarSaathi is on your home screen: in Safari tap Share, then
      <strong> Add to Home Screen</strong>, open it from there and come back to this page.</p>
  }
  if (support === 'none') return <p className="small muted">This browser can't show notifications. Reminders still come by email and in the inbox.</p>
  if (support === 'web' && !prefs.push_available) return <p className="small muted">Notifications on devices are not set up on this server yet.</p>
  const what = support === 'native' ? 'Reminders appear on this phone at 9 am, even when the app is closed.'
    : 'Reminders and booking updates appear on this device, even when CarSaathi is closed.'
  return (
    <div className="device">
      <div className="grow">
        <div className="title">{support === 'native' ? 'This phone' : 'This browser'}: {state === 'on' ? 'on' : state === 'blocked' ? 'blocked' : 'off'}</div>
        <div className="meta">{state === 'blocked' ? 'Notifications are blocked. Allow them in the settings for this site or app, then come back.' : what}</div>
      </div>
      {state !== 'on' && state !== 'blocked' && <button className="btn small" disabled={busy} onClick={turnOn}>{busy ? 'Turning on…' : 'Turn on'}</button>}
      {state === 'on' && support === 'web' && <button className="btn quiet small" disabled={busy} onClick={turnOff}>Turn off</button>}
      {state === 'on' && support === 'native' && <button className="btn quiet small" disabled={busy} onClick={() => syncNative(true).then(check)}>Refresh</button>}
      <ErrorText error={error} />
    </div>
  )
}

export default function NotificationSettings() {
  const [p, setP] = useState(null)
  const [error, setError] = useState('')
  const [msg, setMsg] = useState('')
  const load = () => api('/notifications/prefs').then(setP).catch((e) => setError(e.message))
  useEffect(() => { load() }, [])

  async function save(patch) {
    setError(''); setMsg('')
    try {
      setP(await api('/notifications/prefs', { method: 'PATCH', body: patch }))
      if (pushSupport() === 'native') syncNative(true)
    } catch (e) { setError(e.message) }
  }
  async function removeDevice(d) {
    try { await api(`/push/subscriptions/${d.id}`, { method: 'DELETE' }); load() } catch (e) { setError(e.message) }
  }
  async function test() {
    setError(''); setMsg('')
    try {
      const r = await api('/notifications/test', { method: 'POST' })
      const parts = [r.push === 'sent' && 'to your devices', r.email === 'sent' && 'by email'].filter(Boolean)
      setMsg(parts.length ? `Test sent ${parts.join(' and ')}. It is also in your inbox.` : 'Test added to your inbox. Turn on email or this device to get it there too.')
    } catch (e) { setError(e.message) }
  }

  if (!p) return error ? <p className="error">{error}</p> : <p className="muted">Loading…</p>
  const kinds = new Set(p.kinds)
  return (
    <>
      <p className="sub">Choose what CarSaathi tells you about, and where. Everything also lands in your inbox.</p>
      <h2>What</h2>
      <div className="list toggles">
        {Object.entries(p.kind_labels).map(([k, label]) => (
          <Toggle key={k} label={label} checked={kinds.has(k)}
                  onChange={(on) => save({ kinds: Object.keys(p.kind_labels).filter((x) => (x === k ? on : kinds.has(x))) })} />
        ))}
      </div>

      <h2>Where</h2>
      <div className="list toggles">
        <Toggle label="Email" checked={p.email && p.email_available} disabled={!p.email_available}
                hint={p.email_available ? `One email a day at most, to ${p.email_address}, plus booking updates` : p.email_note}
                onChange={(on) => save({ email: on })} />
        <Toggle label="Notifications on my devices" checked={p.push} onChange={(on) => save({ push: on })}
                hint="On every phone and browser where you turned them on" />
      </div>
      <ThisDevice prefs={p} reload={load} />
      {p.devices.length > 0 && (
        <div className="list" style={{ marginTop: 12 }}>
          {p.devices.map((d) => (
            <div key={d.id} className="row">
              <div className="grow"><div className="title">{d.label}</div><div className="meta">Added {ago(d.created_at)}{d.last_ok_at ? `, last delivered ${ago(d.last_ok_at)}` : ''}</div></div>
              <button className="linkbtn small" onClick={() => removeDevice(d)}>Remove</button>
            </div>
          ))}
        </div>
      )}
      <ErrorText error={error} />
      {msg && <p className="notice good small" role="status">{msg}</p>}
      <div className="actions"><button className="btn quiet" onClick={test}>Send me a test</button></div>
      <p className="small muted">Reminders go out once a day, around 9 am: 30, 7 and 1 day before papers expire, 7 and 1 day before a service
        is due (by date, or by the day your km reach it), on the day, then weekly while overdue.</p>
    </>
  )
}
