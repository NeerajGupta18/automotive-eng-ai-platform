import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api.js'

const KIND = { reminders: 'Reminder', documents: 'Document', health: 'Car health', bookings: 'Service booking', test: 'Test' }

export function ago(iso) {
  if (!iso) return ''
  const t = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`)
  const mins = Math.round((Date.now() - t.getTime()) / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins} min ago`
  const h = Math.round(mins / 60)
  if (h < 24) return `${h} h ago`
  const d = Math.round(h / 24)
  return d === 1 ? 'yesterday' : d < 30 ? `${d} days ago` : t.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })
}

export const unreadChanged = (n) => window.dispatchEvent(new CustomEvent('cs-unread', { detail: n }))

// The inbox: everything the daily run and the workshops sent, newest first. Opening one marks it read and goes to its page.
export default function Notifications() {
  const nav = useNavigate()
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const load = () => api('/notifications').then((d) => { setData(d); unreadChanged(d.unread) }).catch((e) => setError(e.message))
  useEffect(() => { load() }, [])

  async function open(n) {
    if (!n.read_at) await api('/notifications/read', { method: 'POST', body: { ids: [n.id] } }).then((r) => unreadChanged(r.unread)).catch(() => {})
    nav(n.path || '/')
  }
  async function readAll() {
    try { unreadChanged((await api('/notifications/read', { method: 'POST', body: {} })).unread); load() } catch (e) { setError(e.message) }
  }

  if (error) return <p className="error">{error}</p>
  if (!data) return <p className="muted">Loading…</p>
  if (!data.items.length) {
    return (
      <div className="empty">
        <h3>Nothing yet</h3>
        <p>Reminders before papers expire and services fall due, and every reply from a workshop you book, arrive here, by email, and
          as notifications on the devices you choose.</p>
        <Link className="btn" to="/me/notifications">Choose how you're notified</Link>
      </div>
    )
  }
  return (
    <>
      <div className="inbox-head">
        <span className="small muted">{data.unread ? `${data.unread} unread` : 'All read'}</span>
        {data.unread > 0 && <button className="linkbtn small" onClick={readAll}>Mark all read</button>}
        <Link className="linkbtn small" to="/me/notifications">Settings</Link>
      </div>
      <div className="list inbox">
        {data.items.map((n) => (
          <button key={n.id} className={`row note ${n.read_at ? 'read' : 'unread'} k-${n.kind}`} onClick={() => open(n)}>
            <i className="pip" aria-hidden="true" />
            <div className="grow">
              <div className="title">{n.title}</div>
              {n.body && <div className="meta">{n.body}</div>}
              <div className="meta small">{KIND[n.kind] || 'Update'}, {ago(n.created_at)}{n.read_at ? '' : ', unread'}</div>
            </div>
            <span className="go" aria-hidden="true">›</span>
          </button>
        ))}
      </div>
    </>
  )
}
