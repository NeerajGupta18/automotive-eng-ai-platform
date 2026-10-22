import { useEffect, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import Field, { ErrorText, Segment } from '../components/Field.jsx'
import Plate from '../components/Plate.jsx'

const PARTS = { morning: 'Morning', afternoon: 'Afternoon', any: 'Any time' }
const addDays = (iso, n) => { const d = new Date(`${iso}T00:00:00Z`); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10) } // calendar days, no time zone

// Book a service: what needs doing (from the car's own reminders, fault codes and help requests), which workshop, which days.
export default function BookService() {
  const { id } = useParams()
  const [params] = useSearchParams()
  const nav = useNavigate()
  const [car, setCar] = useState(null)
  const [opt, setOpt] = useState(null)
  const [picked, setPicked] = useState(new Set())
  const [extra, setExtra] = useState('')
  const [ws, setWs] = useState('new')
  const [newWs, setNewWs] = useState({ name: '', phone: '', address: '' })
  const [slots, setSlots] = useState([])
  const [pickup, setPickup] = useState(false)
  const [phone, setPhone] = useState('')
  const [odo, setOdo] = useState('')
  const [notes, setNotes] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    api(`/cars/${id}`).then(setCar).catch((e) => setError(e.message))
    api(`/cars/${id}/booking-options`).then((o) => {
      setOpt(o)
      const want = new Set(o.jobs.filter((j) => j.kind === 'reminder' && j.due).map((j) => `${j.kind}:${j.ref}`)) // due reminders ticked
      if (params.get('code')) want.add(`code:${params.get('code').toUpperCase()}`)
      if (params.get('reminder')) want.add(`reminder:${params.get('reminder')}`)
      if (params.get('help')) want.add(`help:${params.get('help')}`)
      setPicked(want)
      if (o.workshops.length) setWs(o.workshops[0].id)
      setSlots([{ on: addDays(o.first_day, 1), part: 'morning' }])
      setPhone(o.contact.phone || '')
      setOdo(o.odometer_km ?? '')
    }).catch((e) => setError(e.message))
  }, [id, params])

  const toggle = (k) => { const s = new Set(picked); s.has(k) ? s.delete(k) : s.add(k); setPicked(s) }
  const setSlot = (i, patch) => setSlots(slots.map((s, j) => (j === i ? { ...s, ...patch } : s)))
  const codeFromLink = params.get('code')?.toUpperCase()

  async function submit(e) {
    e.preventDefault()
    setBusy(true); setError('')
    const jobs = [...picked].map((k) => { const [kind, ...rest] = k.split(':'); return { kind, ref: rest.join(':') } })
    if (extra.trim()) jobs.push({ kind: 'custom', text: extra.trim() })
    const body = { jobs, slots, pickup, contact_phone: phone, notes, odometer_km: odo === '' ? null : odo,
      ...(ws === 'new' ? { workshop: newWs } : { workshop_id: ws }) }
    try {
      const b = await api(`/cars/${id}/bookings`, { method: 'POST', body })
      nav(`/bookings/${b.id}?new=1`, { replace: true })
    } catch (err) { setError(err.message); setBusy(false) }
  }

  if (!opt || !car) return error ? <p className="error">{error}</p> : <p className="muted">Loading…</p>
  const choices = [...opt.jobs]
  if (codeFromLink && !choices.some((j) => j.kind === 'code' && j.ref === codeFromLink)) {
    choices.push({ kind: 'code', ref: codeFromLink, text: `${codeFromLink}: fault code to check` })
  }
  return (
    <form onSubmit={submit} className="booking-form">
      <div className="hero-car compact"><Plate reg={car.reg_display} size="m" /><div className="model">{car.make} {car.model}</div></div>

      <h2>What needs doing</h2>
      {choices.length > 0 && (
        <div className="list checks">
          {choices.map((j) => {
            const k = `${j.kind}:${j.ref}`
            return (
              <label key={k} className="row check">
                <input type="checkbox" checked={picked.has(k)} onChange={() => toggle(k)} />
                <span className="grow"><span className="title">{j.text}</span>
                  <span className="meta">{j.kind === 'reminder' ? (j.status === 'overdue' ? 'Reminder, overdue' : j.due ? 'Reminder, due soon' : 'Reminder')
                    : j.kind === 'code' ? 'Fault code from the health check' : 'Your help request'}</span></span>
              </label>
            )
          })}
        </div>
      )}
      <Field label={choices.length ? 'Anything else?' : 'What should the workshop do?'} hint="In your words, e.g. AC not cooling well">
        <input maxLength={120} value={extra} onChange={(e) => setExtra(e.target.value)} />
      </Field>

      <h2>Workshop</h2>
      {opt.workshops.length > 0 && (
        <div className="list checks">
          {opt.workshops.map((w) => (
            <label key={w.id} className="row check">
              <input type="radio" name="ws" checked={ws === w.id} onChange={() => setWs(w.id)} />
              <span className="grow"><span className="title">{w.name}</span><span className="meta">{[w.phone, w.address].filter(Boolean).join(', ') || 'No number saved'}</span></span>
            </label>
          ))}
          <label className="row check">
            <input type="radio" name="ws" checked={ws === 'new'} onChange={() => setWs('new')} />
            <span className="grow"><span className="title">Another workshop</span></span>
          </label>
        </div>
      )}
      {ws === 'new' && (
        <>
          <Field label="Workshop or service centre"><input required minLength={2} maxLength={80} value={newWs.name} onChange={(e) => setNewWs({ ...newWs, name: e.target.value })} /></Field>
          <div className="two">
            <Field label="Their phone" hint="A mobile number lets you send the request on WhatsApp">
              <input inputMode="tel" maxLength={20} value={newWs.phone} onChange={(e) => setNewWs({ ...newWs, phone: e.target.value })} />
            </Field>
            <Field label="Area"><input maxLength={200} value={newWs.address} onChange={(e) => setNewWs({ ...newWs, address: e.target.value })} /></Field>
          </div>
        </>
      )}

      <h2>When suits you</h2>
      {slots.map((s, i) => (
        <div key={i} className="slot">
          <Field label={i === 0 ? 'First choice' : `Choice ${i + 1}`}>
            <input type="date" required min={opt.first_day} max={opt.last_day} value={s.on} onChange={(e) => setSlot(i, { on: e.target.value })} />
          </Field>
          <Segment label={`Part of the day, choice ${i + 1}`} options={PARTS} value={s.part} onChange={(part) => setSlot(i, { part })} />
          {i > 0 && <button type="button" className="linkbtn small" onClick={() => setSlots(slots.filter((_, j) => j !== i))}>Remove</button>}
        </div>
      ))}
      {slots.length < 3 && (
        <button type="button" className="linkbtn" onClick={() => setSlots([...slots, { on: addDays(slots[slots.length - 1].on, 1), part: 'any' }])}>Add another day</button>
      )}
      <label className="row check plain">
        <input type="checkbox" checked={pickup} onChange={(e) => setPickup(e.target.checked)} />
        <span className="grow"><span className="title">Pick up and drop the car</span><span className="meta">Many workshops do this for a small charge</span></span>
      </label>

      <h2>For the workshop</h2>
      <div className="two">
        <Field label="Mobile number they can call"><input inputMode="tel" required value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="98765 43210" /></Field>
        <Field label="Odometer now (km)"><input inputMode="numeric" value={odo} onChange={(e) => setOdo(e.target.value.replace(/\D/g, ''))} /></Field>
      </div>
      <Field label="Notes"><textarea maxLength={500} value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="E.g. please call before picking up" /></Field>
      <ErrorText error={error} />
      <div className="actions"><button className="btn" disabled={busy || (!picked.size && !extra.trim())}>{busy ? 'Saving…' : 'Create booking request'}</button></div>
      <p className="small muted">Next you send it to the workshop (WhatsApp, SMS or a link). They confirm a time on a page of their own, without an account.</p>
    </form>
  )
}
