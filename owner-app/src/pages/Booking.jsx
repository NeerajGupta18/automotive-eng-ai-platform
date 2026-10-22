import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, download } from '../api.js'
import Field, { ErrorText, Segment } from '../components/Field.jsx'
import Plate from '../components/Plate.jsx'
import Sheet from '../components/Sheet.jsx'
import { niceDate } from '../format.js'
import { ago } from './Notifications.jsx'

const TONE = { requested: 'due_soon', proposed: 'due_soon', confirmed: 'upcoming', in_service: 'upcoming', ready: 'upcoming', completed: 'done',
  cancelled: 'done', declined: 'overdue' }
const ACTIVE = ['requested', 'proposed', 'confirmed', 'in_service', 'ready']
const PARTS = { morning: 'Morning', afternoon: 'Afternoon', any: 'Any time' }
const inr = (n) => `₹${Number(n).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`

export function bookingLine(b) {
  if (b.status === 'confirmed') return `${b.appointment.label} at ${b.workshop.name}`
  if (b.status === 'proposed') return `${b.workshop.name} offers ${b.proposed.label}`
  return `${b.status_label}, ${b.workshop.name}`
}

export function BookingRow({ b, showCar }) {
  return (
    <Link className={`row ${TONE[b.status]}`} to={`/bookings/${b.id}`}>
      <div className="grow">
        <div className="title">{showCar ? `Service: ${b.car.make} ${b.car.model}` : b.workshop.name}</div>
        <div className="meta">{showCar ? bookingLine(b) : b.appointment ? b.appointment.label : b.status === 'completed' && b.bill ? `Bill ${inr(b.bill)}` : niceDate(b.created_at)}</div>
      </div>
      <span className={`when ${TONE[b.status]}`}>{b.status_label}</span>
    </Link>
  )
}

// The car's Service tab: its bookings and the way to make one.
export function CarService({ car }) {
  const [items, setItems] = useState(null)
  const [error, setError] = useState('')
  useEffect(() => { api(`/cars/${car.id}/bookings`).then(setItems).catch((e) => setError(e.message)) }, [car.id])
  if (!items) return error ? <p className="error">{error}</p> : <p className="muted">Loading…</p>
  const open = items.filter((b) => ACTIVE.includes(b.status))
  const past = items.filter((b) => !ACTIVE.includes(b.status))
  return (
    <>
      {open.length > 0 && <div className="list">{open.map((b) => <BookingRow key={b.id} b={b} />)}</div>}
      {!items.length && (
        <div className="empty">
          <h3>Book your next service here</h3>
          <p>Pick what needs doing (due reminders and fault codes are filled in), the workshop and the days that suit you. The workshop
            confirms on a page of its own; the bill lands in Costs and your reminders move on.</p>
        </div>
      )}
      <div className="actions"><Link className="btn" to={`/cars/${car.id}/book`}>Book a service</Link></div>
      {past.length > 0 && (<><h2>Earlier</h2><div className="list">{past.map((b) => <BookingRow key={b.id} b={b} />)}</div></>)}
    </>
  )
}

function Next({ b, shared }) {
  const w = b.workshop.name
  switch (b.status) {
    case 'requested': return shared ? `Waiting for ${w} to confirm a time. You'll get a notification when they answer.`
      : `Send the request to ${w}. They confirm a time on their own page, no app or account needed.`
    case 'proposed': return `${w} can't do your days and offers ${b.proposed.label}.`
    case 'confirmed': return `${b.appointment.label} at ${w}.`
    case 'in_service': return `Your car is at ${w}.`
    case 'ready': return `Your car is ready to collect${b.bill ? `. Bill ${inr(b.bill)}` : ''}.`
    case 'completed': return `Done${b.completed_at ? ` on ${niceDate(b.completed_at)}` : ''}${b.bill ? `. ${inr(b.bill)}${b.expense_id ? ' added to Costs' : ''}` : ''}.`
    case 'declined': return `${w} can't take this booking. Try another workshop or other days.`
    default: return 'This booking was cancelled.'
  }
}

export default function Booking() {
  const { id } = useParams()
  const [params] = useSearchParams()
  const nav = useNavigate()
  const [b, setB] = useState(null)
  const [link, setLink] = useState(null)
  const [sheet, setSheet] = useState(null)
  const [error, setError] = useState('')
  const load = useCallback(() => api(`/bookings/${id}`).then(setB).catch((e) => setError(e.message)), [id])
  useEffect(() => { load() }, [load])

  async function act(path, body) {
    setError('')
    try { setB(await api(`/bookings/${id}/${path}`, { method: 'POST', body: body || {} })) } catch (e) { setError(e.message) }
  }
  async function makeLink() {
    setError('')
    try { setLink(await api(`/bookings/${id}/share`, { method: 'POST' })); load() } catch (e) { setError(e.message) }
  }

  if (!b) return error ? <p className="error">{error}</p> : <p className="muted">Loading…</p>
  const active = ACTIVE.includes(b.status)
  const url = link ? (link.url || `${window.location.origin}${link.path}`) : ''
  const message = link ? link.message.replace('{link}', url) : ''
  return (
    <>
      <section className={`bstatus ${TONE[b.status]}`}>
        <div className="grow">
          <h2>{b.status_label}</h2>
          <p><Next b={b} shared={b.shared} /></p>
        </div>
      </section>
      {params.get('new') && !link && b.status === 'requested' && !b.shared && <p className="notice good">Booking request saved. Now send it to the workshop.</p>}

      {b.status === 'proposed' && (
        <div className="actions">
          <button className="btn" onClick={() => act('accept')}>Accept {b.proposed.label}</button>
          <button className="btn quiet" onClick={() => setSheet('days')}>Choose other days</button>
        </div>
      )}
      {b.status === 'confirmed' && (
        <div className="actions">
          <button className="btn" onClick={() => download(`/bookings/${id}/calendar.ics`, 'car-service.ics').catch((e) => setError(e.message))}>Add to calendar</button>
          <button className="btn quiet" onClick={() => act('handed-over')}>I've left the car there</button>
        </div>
      )}
      {['confirmed', 'in_service', 'ready'].includes(b.status) && (
        <div className="actions"><button className={`btn${b.status === 'ready' ? '' : ' quiet'}`} onClick={() => setSheet('complete')}>Service done</button></div>
      )}

      {active && b.shared && !link && !['requested', 'proposed'].includes(b.status) && (
        <p className="small linkrow">The workshop has a private link to this booking.
          <button className="linkbtn small" onClick={makeLink}>Make a new link</button>
          <button className="linkbtn small" onClick={() => act('unshare')}>Turn it off</button></p>
      )}
      {active && (link || !b.shared || ['requested', 'proposed'].includes(b.status)) && (
        <section className="sendcard">
          <h2>Send to the workshop</h2>
          {!link && (
            <>
              <p className="small">{b.shared ? 'A link was made earlier. Make a new one to send it again; the earlier link stops working.'
                : 'This makes a private link to this booking for the workshop. It works for 30 days, needs no account, and you can turn it off.'}</p>
              <div className="actions">
                <button className="btn" onClick={makeLink}>{b.shared ? 'Make a new link' : 'Make the link'}</button>
                {b.shared && <button className="btn quiet" onClick={() => act('unshare')}>Turn the link off</button>}
              </div>
            </>
          )}
          {link && (
            <>
              <p className="small">The workshop sees {link.shared_data.join(', ')}. Nothing else from your account.</p>
              <div className="actions">
                {link.whatsapp_number && (
                  <a className="btn" href={`https://wa.me/${link.whatsapp_number}?text=${encodeURIComponent(message)}`} target="_blank" rel="noreferrer">Send on WhatsApp</a>
                )}
                {b.workshop.phone && <a className="btn quiet" href={`sms:${b.workshop.phone.replace(/[^\d+]/g, '')}?body=${encodeURIComponent(message)}`}>SMS</a>}
                {navigator.share && <button className="btn quiet" onClick={() => navigator.share({ text: message }).catch(() => {})}>Share…</button>}
                <button className="btn quiet" onClick={() => navigator.clipboard?.writeText(message).then(() => setLink({ ...link, copied: true }))}>{link.copied ? 'Copied' : 'Copy message'}</button>
              </div>
              <p className="small"><a href={url} target="_blank" rel="noreferrer">See what the workshop sees</a></p>
            </>
          )}
          {link && <button className="linkbtn small" onClick={() => act('unshare').then(() => setLink(null))}>Turn the link off</button>}
        </section>
      )}

      <h2>Job card</h2>
      <div className="jobcard">
        <div className="jc-head"><Plate reg={b.car.reg_display} size="s" /><span>{b.car.make} {b.car.model}{b.car.odometer_km ? `, ${b.car.odometer_km.toLocaleString('en-IN')} km` : ''}</span></div>
        <ol>{b.jobs.map((j, i) => <li key={i}>{j.text}</li>)}</ol>
        <dl className="facts">
          <dt>Workshop</dt><dd>{b.workshop.name}{b.workshop.phone && <>, <a href={`tel:${b.workshop.phone.replace(/[^\d+]/g, '')}`}>{b.workshop.phone}</a></>}</dd>
          {!b.appointment && <><dt>Preferred</dt><dd>{b.slots.map((s) => s.label).join('; ')}</dd></>}
          <dt>Pick up</dt><dd>{b.pickup ? 'Yes, pick up and drop' : 'No, I bring the car'}</dd>
          <dt>Contact</dt><dd>{b.contact.name}, {b.contact.phone}</dd>
          {b.estimate != null && <><dt>Estimate</dt><dd>{inr(b.estimate)}{b.workshop_note ? ` (${b.workshop_note})` : ''}</dd></>}
          {b.bill != null && <><dt>Bill</dt><dd>{inr(b.bill)}</dd></>}
          {b.notes && <><dt>Notes</dt><dd>{b.notes}</dd></>}
        </dl>
      </div>

      <h2>Updates</h2>
      <ol className="timeline">
        {[...b.events].reverse().map((e, i) => (
          <li key={i} className={e.actor}><strong>{e.actor === 'workshop' ? b.workshop.name : e.actor === 'owner' ? 'You' : 'CarSaathi'}</strong> {e.detail}
            <span className="meta small"> {ago(e.at)}</span></li>
        ))}
      </ol>

      <ErrorText error={error} />
      <div className="actions">
        {active && <button className="btn quiet small" onClick={() => setSheet('cancel')}>Cancel booking</button>}
        {!active && <button className="btn quiet small" onClick={() => api(`/bookings/${id}`, { method: 'DELETE' }).then(() => nav(`/cars/${b.car.id}?tab=service`, { replace: true })).catch((e) => setError(e.message))}>Delete</button>}
        <Link className="btn quiet small" to={`/cars/${b.car.id}?tab=service`}>All bookings of this car</Link>
      </div>

      {sheet === 'complete' && <CompleteSheet b={b} onClose={() => setSheet(null)} onDone={(x) => { setB(x); setSheet(null) }} />}
      {sheet === 'cancel' && <CancelSheet onClose={() => setSheet(null)} onCancel={(note) => act('cancel', { note }).then(() => setSheet(null))} />}
      {sheet === 'days' && <DaysSheet b={b} onClose={() => setSheet(null)} onSave={(slots) => act('reschedule', { slots }).then(() => setSheet(null))} />}
    </>
  )
}

function CompleteSheet({ b, onClose, onDone }) {
  const rems = b.jobs.filter((j) => j.kind === 'reminder')
  const [amount, setAmount] = useState(b.bill ?? b.estimate ?? '')
  const [odo, setOdo] = useState(b.car.odometer_km ?? '')
  const [addExpense, setAddExpense] = useState(true)
  const [done, setDone] = useState(new Set(rems.map((j) => j.ref)))
  const [error, setError] = useState('')
  async function save(e) {
    e.preventDefault()
    try {
      onDone(await api(`/bookings/${b.id}/complete`, { method: 'POST', body: { amount: amount === '' ? null : amount, odometer_km: odo === '' ? null : odo,
        add_expense: addExpense, done_reminders: [...done] } }))
    } catch (err) { setError(err.message) }
  }
  return (
    <Sheet title="Service done" onClose={onClose}>
      <form onSubmit={save}>
        <div className="two">
          <Field label="Bill (₹)"><input inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} /></Field>
          <Field label="Odometer (km)"><input inputMode="numeric" value={odo} onChange={(e) => setOdo(e.target.value.replace(/\D/g, ''))} /></Field>
        </div>
        <label className="row check plain"><input type="checkbox" checked={addExpense} onChange={(e) => setAddExpense(e.target.checked)} />
          <span className="grow"><span className="title">Add the bill to Costs</span><span className="meta">As a service expense from {b.workshop.name}</span></span></label>
        {rems.length > 0 && (
          <>
            <span className="field"><span>Mark these reminders done</span></span>
            {rems.map((j) => (
              <label key={j.ref} className="row check plain">
                <input type="checkbox" checked={done.has(j.ref)} onChange={() => { const s = new Set(done); s.has(j.ref) ? s.delete(j.ref) : s.add(j.ref); setDone(s) }} />
                <span className="grow"><span className="title">{j.text}</span><span className="meta">A repeating one moves to its next date and km</span></span>
              </label>
            ))}
          </>
        )}
        <ErrorText error={error} />
        <div className="actions"><button className="btn">Save</button></div>
      </form>
    </Sheet>
  )
}

function CancelSheet({ onClose, onCancel }) {
  const [note, setNote] = useState('')
  return (
    <Sheet title="Cancel this booking?" onClose={onClose}>
      <Field label="Reason (the workshop sees it)" hint="Optional"><input maxLength={200} value={note} onChange={(e) => setNote(e.target.value)} /></Field>
      <p className="small">The workshop's link stops working.</p>
      <div className="actions"><button className="btn danger" onClick={() => onCancel(note)}>Cancel booking</button><button className="btn quiet" onClick={onClose}>Keep it</button></div>
    </Sheet>
  )
}

function DaysSheet({ b, onClose, onSave }) {
  const [slots, setSlots] = useState(b.slots.map((s) => ({ on: s.on, part: s.part })))
  const set = (i, patch) => setSlots(slots.map((s, j) => (j === i ? { ...s, ...patch } : s)))
  return (
    <Sheet title="Other days" onClose={onClose}>
      {slots.map((s, i) => (
        <div key={i} className="slot">
          <Field label={`Choice ${i + 1}`}><input type="date" value={s.on} onChange={(e) => set(i, { on: e.target.value })} /></Field>
          <Segment label={`Part of the day, choice ${i + 1}`} options={PARTS} value={s.part} onChange={(part) => set(i, { part })} />
        </div>
      ))}
      {slots.length < 3 && <button type="button" className="linkbtn" onClick={() => setSlots([...slots, { ...slots[slots.length - 1], part: 'any' }])}>Add another day</button>}
      <p className="small">The workshop sees the new days on its page; send them a message so they look again.</p>
      <div className="actions"><button className="btn" onClick={() => onSave(slots)}>Save days</button></div>
    </Sheet>
  )
}
