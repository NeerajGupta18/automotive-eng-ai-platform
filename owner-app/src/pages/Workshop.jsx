import { useEffect, useState } from 'react'
import Plate from '../components/Plate.jsx'

// The workshop's side of a booking (Phase D): opened from the private link the owner sent (/w#<token>), no account or app.
// The token stays in the URL fragment, which the browser never sends to a server; this page sends it in a header.
const PARTS = { morning: ['08:00', '12:59', '10:00'], afternoon: ['13:00', '19:59', '15:00'], any: ['07:00', '21:00', '11:00'] }
const inr = (n) => `₹${Number(n).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`
const FUEL = { petrol: 'petrol', diesel: 'diesel', cng: 'CNG', electric: 'electric', hybrid: 'hybrid' }
// the status in the workshop's own words (the owner's app words it from the owner's side)
const HEADING = { requested: 'New booking request', proposed: 'Time offered', confirmed: 'Confirmed', in_service: 'Car with you',
  ready: 'Ready for collection', completed: 'Collected', cancelled: 'Cancelled', declined: 'Declined' }
const today = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}` }

export function fits(slots, on, time) {
  return slots.some((s) => s.on === on && PARTS[s.part][0] <= time && time <= PARTS[s.part][1])
}

async function call(token, path = '', body) {
  let res
  try {
    res = await fetch(`/api/w/booking${path}`, {
      method: body ? 'POST' : 'GET', body: body ? JSON.stringify(body) : undefined,
      headers: { 'X-Booking-Token': token, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    })
  } catch { throw new Error('No connection. Check your internet and try again.') }
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    const d = data.detail
    throw Object.assign(new Error(Array.isArray(d) ? d.map((x) => x.msg).join('; ') : d || 'Something went wrong. Try again.'), { status: res.status })
  }
  return data
}

function Panel({ title, children, onClose }) {
  return (
    <section className="wpanel">
      <header><h2>{title}</h2><button type="button" className="linkbtn small" onClick={onClose}>Close</button></header>
      {children}
    </section>
  )
}

function ConfirmPanel({ b, send, onClose, change }) {
  const first = b.slots[0]
  const [pick, setPick] = useState(change || !first ? 'other' : 0)
  const [on, setOn] = useState(change ? b.appointment.on : first ? first.on : today())
  const [time, setTime] = useState(change ? b.appointment.time : first ? PARTS[first.part][2] : '10:00')
  const [note, setNote] = useState('')
  const choose = (i) => { setPick(i); if (i !== 'other') { setOn(b.slots[i].on); setTime(PARTS[b.slots[i].part][2]) } }
  const ok = fits(b.slots, on, time)
  return (
    <Panel title={change ? 'Change the time' : 'Confirm a time'} onClose={onClose}>
      <form onSubmit={(e) => { e.preventDefault(); send('confirm', { on, time, note }) }}>
        {b.slots.length > 0 && (
          <div className="segment" role="group" aria-label="Day">
            {b.slots.map((s, i) => <button type="button" key={s.on + s.part} aria-pressed={pick === i} onClick={() => choose(i)}>{s.label}</button>)}
            <button type="button" aria-pressed={pick === 'other'} onClick={() => setPick('other')}>Another day</button>
          </div>
        )}
        <div className="two">
          <label className="field"><span>Date</span>
            <input type="date" required min={today()} value={on} disabled={pick !== 'other'} onChange={(e) => setOn(e.target.value)} /></label>
          <label className="field"><span>Time</span>
            <input type="time" required min="07:00" max="21:00" step="900" value={time} onChange={(e) => setTime(e.target.value)} /></label>
        </div>
        <label className="field"><span>Message (optional)</span><input maxLength={300} value={note} onChange={(e) => setNote(e.target.value)} placeholder="E.g. bring the service book" /></label>
        <p className={`small ${ok ? '' : 'warnline'}`}>{ok ? `${b.contact.name} gets this as confirmed.` : `This is outside ${b.contact.name}'s preferred days, so it goes as an offer they accept.`}</p>
        <button className="btn block">{ok ? 'Confirm this time' : 'Offer this time'}</button>
      </form>
    </Panel>
  )
}

function AmountPanel({ title, label, action, button, required, send, onClose, initial }) {
  const [amount, setAmount] = useState(initial ?? '')
  const [note, setNote] = useState('')
  return (
    <Panel title={title} onClose={onClose}>
      <form onSubmit={(e) => { e.preventDefault(); send(action, { amount: amount === '' ? null : amount, note }) }}>
        <label className="field"><span>{label}</span><input inputMode="decimal" required={required} value={amount} onChange={(e) => setAmount(e.target.value.replace(/[^\d.]/g, ''))} placeholder="₹" /></label>
        <label className="field"><span>Note (optional)</span><input maxLength={300} value={note} onChange={(e) => setNote(e.target.value)}
          placeholder={action === 'estimate' ? 'E.g. includes oil and filter; brake pads extra if needed' : 'E.g. open till 8 pm'} /></label>
        <button className="btn block">{button}</button>
      </form>
    </Panel>
  )
}

function TextPanel({ title, label, action, button, required, danger, send, onClose }) {
  const [note, setNote] = useState('')
  return (
    <Panel title={title} onClose={onClose}>
      <form onSubmit={(e) => { e.preventDefault(); send(action, { note }) }}>
        <label className="field"><span>{label}</span><textarea maxLength={300} required={required} value={note} onChange={(e) => setNote(e.target.value)} /></label>
        <button className={`btn block${danger ? ' danger' : ''}`}>{button}</button>
      </form>
    </Panel>
  )
}

export default function Workshop() {
  const token = decodeURIComponent(window.location.hash.slice(1))
  const [b, setB] = useState(null)
  const [error, setError] = useState('')
  const [gone, setGone] = useState('')
  const [panel, setPanel] = useState(null)
  const [busy, setBusy] = useState(false)
  const [sent, setSent] = useState('')

  useEffect(() => {
    document.title = 'Service booking request'
    if (!token) { setGone('This page needs the full link the car owner sent you.'); return }
    call(token).then(setB).catch((e) => (e.status === 404 ? setGone(e.message) : setError(e.message)))
  }, [token])

  async function send(action, body) {
    setBusy(true); setError(''); setSent('')
    try {
      setB(await call(token, `/${action}`, body))
      setPanel(null)
      setSent({ confirm: 'Sent.', estimate: 'Estimate sent.', message: 'Message sent.', received: 'Marked as received.', ready: 'Marked as ready.',
        decline: 'The owner has been told.' }[action] + ' The owner gets a notification.')
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const shell = (children) => (
    <div className="wpage">
      <header className="wtop"><span className="wordmark"><i aria-hidden="true" />CarSaathi</span><span className="small muted">Service booking</span></header>
      {children}
    </div>
  )
  if (gone) return shell(<div className="empty"><h3>Link not valid</h3><p>{gone}</p></div>)
  if (!b) return shell(error ? <p className="error">{error}</p> : <p className="muted">Loading…</p>)

  const can = new Set(b.can)
  const name = b.contact.name || 'The owner'
  const phone = b.contact.phone.replace(/[^\d+]/g, '')
  const head = {
    requested: `${name} asks for a service slot. Confirm a time, or offer another.`,
    proposed: `You offered ${b.proposed?.label}. Waiting for ${name} to accept.`,
    confirmed: `Confirmed for ${b.appointment?.label}.`,
    in_service: 'The car is with you.',
    ready: `Marked ready${b.bill != null ? `, bill ${inr(b.bill)}` : ''}. ${name} will collect it.`,
    completed: `${name} has collected the car. Thank you.`,
    cancelled: `${name} cancelled this booking.`,
    declined: 'You said you can\'t take this booking.',
  }[b.status]
  return shell(
    <>
      <section className={`wstatus s-${b.status}`}><h1>{HEADING[b.status] || b.status_label}</h1><p>{head}</p></section>
      {sent && <p className="notice good small" role="status">{sent}</p>}
      {error && <p className="error" role="alert">{error}</p>}

      {b.status === 'requested' && can.has('confirm') && !panel && (
        <div className="actions"><button className="btn" onClick={() => setPanel('confirm')}>Confirm a time</button>
          <a className="btn quiet" href={`tel:${phone}`}>Call {name}</a></div>
      )}
      {panel === 'confirm' && <ConfirmPanel b={b} send={send} onClose={() => setPanel(null)} />}
      {panel === 'change' && <ConfirmPanel b={b} send={send} onClose={() => setPanel(null)} change />}
      {panel === 'estimate' && <AmountPanel title="Send an estimate" label="Estimate (₹)" action="estimate" button="Send estimate" required send={send} onClose={() => setPanel(null)} initial={b.estimate} />}
      {panel === 'ready' && <AmountPanel title="Car ready" label="Bill (₹, optional)" action="ready" button="Mark ready" send={send} onClose={() => setPanel(null)} initial={b.estimate} />}
      {panel === 'message' && <TextPanel title={`Message to ${name}`} label="Message" action="message" button="Send message" required send={send} onClose={() => setPanel(null)} />}
      {panel === 'decline' && <TextPanel title="Can't take it?" label={`Tell ${name} why (optional)`} action="decline" button="Can't take this booking" danger send={send} onClose={() => setPanel(null)} />}

      <section className="jobcard wcard">
        <div className="jc-head"><Plate reg={b.car.reg_display} size="m" /></div>
        <div className="jc-car">{b.car.make} {b.car.model}{b.car.variant ? ` ${b.car.variant}` : ''}<span className="muted">
          {[FUEL[b.car.fuel], b.car.year, b.car.odometer_km != null && `${b.car.odometer_km.toLocaleString('en-IN')} km`].filter(Boolean).join(', ')}</span></div>
        <h2>Work requested</h2>
        <ol className="jobs">{b.jobs.map((j, i) => <li key={i}><strong>{j.text}</strong>{j.detail && <span className="meta">{j.detail}</span>}</li>)}</ol>
        <dl className="facts">
          {!b.appointment && <><dt>Preferred</dt><dd>{b.slots.map((s) => s.label).join('; ')}</dd></>}
          {b.appointment && <><dt>Appointment</dt><dd>{b.appointment.label}</dd></>}
          <dt>Pick up</dt><dd>{b.pickup ? 'Yes, please pick up and drop' : 'No, they bring the car'}</dd>
          <dt>Contact</dt><dd>{name}, <a href={`tel:${phone}`}>{b.contact.phone}</a></dd>
          {b.estimate != null && <><dt>Your estimate</dt><dd>{inr(b.estimate)}{b.workshop_note ? ` (${b.workshop_note})` : ''}</dd></>}
          {b.notes && <><dt>Notes</dt><dd>{b.notes}</dd></>}
        </dl>
      </section>

      {can.size > 0 && !panel && (
        <div className="wactions">
          {b.status !== 'requested' && can.has('confirm') && <button className="btn quiet" onClick={() => setPanel(b.status === 'confirmed' ? 'change' : 'confirm')}>{b.status === 'confirmed' ? 'Change the time' : 'Confirm a time'}</button>}
          {can.has('received') && <button className="btn" disabled={busy} onClick={() => send('received', {})}>Car received</button>}
          {can.has('ready') && <button className={`btn${b.status === 'in_service' ? '' : ' quiet'}`} onClick={() => setPanel('ready')}>Car ready</button>}
          {can.has('estimate') && <button className="btn quiet" onClick={() => setPanel('estimate')}>{b.estimate != null ? 'Update estimate' : 'Send an estimate'}</button>}
          {can.has('message') && <button className="btn quiet" onClick={() => setPanel('message')}>Message</button>}
          {can.has('decline') && <button className="btn quiet" onClick={() => setPanel('decline')}>Can't take it</button>}
        </div>
      )}

      <h2>Updates</h2>
      <ol className="timeline">
        {[...b.events].reverse().map((e, i) => (
          <li key={i} className={e.actor}><strong>{e.actor === 'workshop' ? 'You' : e.actor === 'owner' ? name : 'CarSaathi'}</strong> {e.detail}
            <span className="meta small"> {new Date(/[zZ]|[+-]\d\d:\d\d$/.test(e.at) ? e.at : `${e.at}Z`).toLocaleString('en-IN', { day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' })}</span></li>
        ))}
      </ol>
      <p className="small muted wfoot">{name} sent you this private link from the CarSaathi app. It shows only this booking and stops working when the
        booking ends. No account or app is needed.</p>
    </>,
  )
}
