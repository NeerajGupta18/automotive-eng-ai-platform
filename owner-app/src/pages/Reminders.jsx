import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { AttentionRow } from './Home.jsx'

const GROUPS = [['overdue', 'Overdue'], ['due_soon', 'In the next 30 days'], ['upcoming', 'Later']]

export default function Reminders() {
  const [items, setItems] = useState(null)
  const [error, setError] = useState('')
  useEffect(() => { api('/reminders').then(setItems).catch((e) => setError(e.message)) }, [])
  if (error) return <p className="error">{error}</p>
  if (!items) return <p className="muted">Loading…</p>
  if (!items.length) {
    return (
      <div className="empty">
        <h3>No reminders yet</h3>
        <p>Documents with an expiry date and the reminders you add to a car show up here, most urgent first.</p>
        <Link className="btn" to="/garage">Open your garage</Link>
      </div>
    )
  }
  return GROUPS.map(([k, label]) => {
    const rows = items.filter((i) => i.status === k)
    return rows.length ? (
      <section key={k}>
        <h2>{label}</h2>
        <div className="list">{rows.map((i) => <AttentionRow key={i.type + i.id} item={i} />)}</div>
      </section>
    ) : null
  })
}
