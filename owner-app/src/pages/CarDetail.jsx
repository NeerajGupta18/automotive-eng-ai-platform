import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, download } from '../api.js'
import { useAuth } from '../auth.jsx'
import { FUELS, km, niceDate, when } from '../format.js'
import Field, { ErrorText, Segment } from '../components/Field.jsx'
import Plate from '../components/Plate.jsx'
import Sheet from '../components/Sheet.jsx'
import CarCosts from './CarCosts.jsx'
import CarHealth from './CarHealth.jsx'
import { CarService } from './Booking.jsx'

const KINDS = { rc: 'RC', insurance: 'Insurance', puc: 'PUC', warranty: 'Warranty', service: 'Service record', fastag: 'FASTag', other: 'Other' }
const TITLES = { rc: 'Registration certificate (RC)', insurance: 'Insurance', puc: 'PUC certificate', warranty: 'Warranty',
  service: 'Service record', fastag: 'FASTag', other: '' }
const REPEAT = { none: 'Once', monthly: 'Every month', quarterly: 'Every 3 months', yearly: 'Every year' }
const IDEAS = ['Periodic service', 'Tyre rotation', 'Wheel alignment', 'Battery check', 'Coolant top-up', 'Wiper blades']
const DOC_STATUS = { expired: 'Expired', expiring: 'Expiring soon', valid: 'Valid', no_expiry: 'No expiry date' }

export default function CarDetail() {
  const { id } = useParams()
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') || 'documents'
  const [car, setCar] = useState(null)
  const [error, setError] = useState('')
  const [sheet, setSheet] = useState(null)
  const bar = useRef(null)
  const [more, setMore] = useState(false)
  const edge = () => { const el = bar.current; if (el) setMore(el.scrollLeft + el.clientWidth < el.scrollWidth - 4) }
  // the chosen tab in view (links open ?tab=service, which can sit past the screen's edge), and a fade while more tabs are hidden
  useEffect(() => {
    bar.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ inline: 'nearest', block: 'nearest' })
    edge()
  }, [tab, car])
  const load = useCallback(() => api(`/cars/${id}`).then(setCar).catch((e) => setError(e.message)), [id])
  useEffect(() => { load() }, [load])
  if (error) return <p className="error">{error}</p>
  if (!car) return <p className="muted">Loading…</p>
  const owner = car.role === 'owner'
  const tabs = { documents: 'Documents', reminders: 'Reminders', health: 'Health', costs: 'Costs', service: 'Service', people: 'People', details: 'Details' }
  const bump = () => window.dispatchEvent(new Event('cs-reload'))

  return (
    <>
      <div className="hero-car">
        <Plate reg={car.reg_display} size="l" />
        <div className="model">{car.make} {car.model}{car.variant ? ` ${car.variant}` : ''}</div>
        <div className="odo">
          <span>{FUELS[car.fuel]}{car.year ? `, ${car.year}` : ''}</span>
          {car.odometer_km != null && <strong>{km(car.odometer_km)}</strong>}
          <button className="linkbtn" onClick={() => setSheet({ type: 'odo' })}>Update km</button>
          <Link className="linkbtn" to={`/help?car=${car.id}`} style={{ marginLeft: 'auto' }}>Get help</Link>
        </div>
      </div>
      {params.get('new') && <p className="notice good" style={{ marginTop: 14 }}>Car added. Add its RC, insurance and PUC to get reminders before they expire.</p>}
      <div className={`tabbar${more ? ' more' : ''}`} role="tablist" ref={bar} onScroll={edge}>
        {Object.entries(tabs).map(([k, v]) => (
          <button key={k} role="tab" aria-selected={tab === k} onClick={() => setParams({ tab: k }, { replace: true })}>{v}</button>
        ))}
      </div>
      {tab === 'documents' && <Documents car={car} openSheet={setSheet} />}
      {tab === 'reminders' && <CarReminders car={car} openSheet={setSheet} />}
      {tab === 'health' && <CarHealth car={car} goTab={(t) => setParams({ tab: t }, { replace: true })} />}
      {tab === 'costs' && <CarCosts car={car} onCarChanged={load} />}
      {tab === 'service' && <CarService car={car} />}
      {tab === 'people' && <People car={car} openSheet={setSheet} />}
      {tab === 'details' && <Details car={car} owner={owner} openSheet={setSheet} />}

      {sheet?.type === 'odo' && <OdometerSheet car={car} onClose={() => setSheet(null)} onSaved={(c) => { setCar(c); setSheet(null) }} />}
      {sheet?.type === 'doc' && <DocumentSheet car={car} doc={sheet.doc} onClose={() => setSheet(null)} onSaved={() => { setSheet(null); setParams({ tab: 'documents' }, { replace: true }); bump() }} />}
      {sheet?.type === 'rem' && <ReminderSheet car={car} rem={sheet.rem} onClose={() => setSheet(null)} onSaved={() => { setSheet(null); bump() }} />}
      {sheet?.type === 'invite' && <InviteSheet car={car} onClose={() => setSheet(null)} onSaved={() => { setSheet(null); bump() }} />}
      {sheet?.type === 'remove' && <RemoveSheet car={car} onClose={() => setSheet(null)} />}
    </>
  )
}

function useReload(fn) {
  useEffect(() => {
    fn()
    window.addEventListener('cs-reload', fn)
    return () => window.removeEventListener('cs-reload', fn)
  }, [fn])
}

// ---------------------------------------------------------------- documents
function Documents({ car, openSheet }) {
  const [docs, setDocs] = useState(null)
  const [error, setError] = useState('')
  const load = useCallback(() => { api(`/cars/${car.id}/documents`).then(setDocs).catch((e) => setError(e.message)) }, [car.id])
  useReload(load)
  if (error) return <p className="error">{error}</p>
  if (!docs) return <p className="muted">Loading…</p>
  const missing = ['rc', 'insurance', 'puc'].filter((k) => !(k === 'puc' && car.fuel === 'electric') && !docs.some((d) => d.kind === k))
  return (
    <>
      {docs.length > 0 && (
        <div className="list">
          {docs.map((d) => (
            <button key={d.id} className={`row ${d.status}`} onClick={() => openSheet({ type: 'doc', doc: d })}>
              <div className="grow">
                <div className="title">{d.title}</div>
                <div className="meta">
                  {d.expires_on ? `${d.status === 'expired' ? 'Expired on' : 'Valid until'} ${niceDate(d.expires_on)}` : DOC_STATUS.no_expiry}
                  {d.has_file ? ', photo attached' : ''}
                </div>
              </div>
              <span className={`when ${d.status}`}>{d.expires_on ? when({ ...d, due_on: d.expires_on }) : ''}</span>
            </button>
          ))}
        </div>
      )}
      {missing.length > 0 && (
        <div className="empty" style={{ marginTop: docs.length ? 14 : 0 }}>
          <h3>Still missing: {missing.map((k) => KINDS[k]).join(', ')}</h3>
          <p>Take a photo of each and enter its validity date. You get a reminder 30 days before it expires.</p>
        </div>
      )}
      <div className="actions"><button className="btn" onClick={() => openSheet({ type: 'doc', doc: null })}>Add document</button></div>
    </>
  )
}

function DocumentSheet({ car, doc, onClose, onSaved }) {
  const editing = !!doc
  const [f, setF] = useState(() => ({
    kind: doc?.kind || 'insurance', title: doc?.title || TITLES.insurance, number: doc?.number || '', issuer: doc?.issuer || '',
    issued_on: doc?.issued_on || '', expires_on: doc?.expires_on || '', notes: doc?.notes || '',
  }))
  const [file, setFile] = useState(null)
  const [preview, setPreview] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })

  useEffect(() => {
    let url
    if (doc?.has_file && doc.file_type?.startsWith('image/')) {
      api(`/documents/${doc.id}/file`, { raw: true }).then((r) => r.blob()).then((b) => { url = URL.createObjectURL(b); setPreview(url) }).catch(() => {})
    }
    return () => url && URL.revokeObjectURL(url)
  }, [doc])

  async function save(e) {
    e.preventDefault()
    setBusy(true); setError('')
    const form = new FormData()
    Object.entries(f).forEach(([k, v]) => form.append(k, v))
    if (file) form.append('file', file)
    try {
      await api(editing ? `/documents/${doc.id}` : `/cars/${car.id}/documents`, { method: editing ? 'PATCH' : 'POST', form })
      onSaved()
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  async function remove() {
    if (!window.confirm(`Delete "${doc.title}"?`)) return
    try { await api(`/documents/${doc.id}`, { method: 'DELETE' }); onSaved() } catch (err) { setError(err.message) }
  }

  return (
    <Sheet title={editing ? doc.title : 'Add a document'} onClose={onClose}>
      <form onSubmit={save}>
        {preview && <img className="preview" src={preview} alt={`Photo of ${doc.title}`} />}
        {doc?.has_file && !preview && (
          <p><button type="button" className="linkbtn" onClick={() => download(`/documents/${doc.id}/file`, doc.file_name)}>Download {doc.file_name}</button></p>
        )}
        <span className="field"><span>Type</span></span>
        <Segment label="Document type" options={KINDS} value={f.kind}
                 onChange={(k) => setF({ ...f, kind: k, title: !f.title || Object.values(TITLES).includes(f.title) ? TITLES[k] : f.title })} />
        <Field label="Title"><input required minLength={2} maxLength={80} value={f.title} onChange={set('title')} /></Field>
        <div className="two">
          <Field label="Issued on"><input type="date" value={f.issued_on} onChange={set('issued_on')} /></Field>
          <Field label="Valid until" hint={f.kind === 'rc' ? 'Optional for the RC' : 'Used for the reminder'}><input type="date" value={f.expires_on} onChange={set('expires_on')} /></Field>
        </div>
        <div className="two">
          <Field label="Number"><input maxLength={40} value={f.number} onChange={set('number')} placeholder="Policy or certificate no." /></Field>
          <Field label="Issued by"><input maxLength={60} value={f.issuer} onChange={set('issuer')} /></Field>
        </div>
        <Field label={editing && doc.has_file ? 'Replace the photo or PDF' : 'Photo or PDF'} hint="JPEG, PNG, WebP or PDF up to 5 MB">
          <input type="file" accept="image/*,application/pdf" capture="environment" onChange={(e) => setFile(e.target.files[0] || null)} />
        </Field>
        <Field label="Notes"><textarea maxLength={500} value={f.notes} onChange={set('notes')} /></Field>
        <ErrorText error={error} />
        <div className="actions">
          <button className="btn" disabled={busy}>{busy ? 'Saving…' : editing ? 'Save changes' : 'Add document'}</button>
          {editing && <button type="button" className="btn quiet" onClick={remove}>Delete</button>}
        </div>
      </form>
    </Sheet>
  )
}

// ---------------------------------------------------------------- reminders
function CarReminders({ car, openSheet }) {
  const [items, setItems] = useState(null)
  const [error, setError] = useState('')
  const load = useCallback(() => { api(`/reminders?car_id=${car.id}`).then(setItems).catch((e) => setError(e.message)) }, [car.id])
  useReload(load)
  async function done(i) {
    try { await api(`/reminders/${i.id}/done`, { method: 'POST' }); load() } catch (err) { setError(err.message) }
  }
  if (!items) return error ? <p className="error">{error}</p> : <p className="muted">Loading…</p>
  const own = items.filter((i) => i.type === 'reminder')
  return (
    <>
      <ErrorText error={error} />
      {own.length > 0 ? (
        <div className="list">
          {own.map((i) => (
            <div key={i.id} className={`row ${i.status}`}>
              <button className="grow linkbtn" style={{ textAlign: 'left', color: 'inherit', fontWeight: 400 }} onClick={() => openSheet({ type: 'rem', rem: i })}>
                <div className="title">{i.title}</div>
                <div className="meta">
                  {[i.due_on && niceDate(i.due_on), i.due_km != null && `at ${km(i.due_km)}`, i.repeat !== 'none' && REPEAT[i.repeat]?.toLowerCase(),
                    i.every_km && `every ${km(i.every_km)}`].filter(Boolean).join(', ')}
                </div>
              </button>
              <div style={{ textAlign: 'right' }}>
                <div className={`when ${i.status}`}>{when(i)}</div>
                <span className="linkrow end">
                  {(i.status === 'overdue' || i.status === 'due_soon') && <Link className="linkbtn small" to={`/cars/${car.id}/book?reminder=${i.id}`}>Book</Link>}
                  <button className="linkbtn small" onClick={() => done(i)}>Mark done</button>
                </span>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="empty"><h3>No reminders for this car</h3><p>Add service, tyre or battery reminders by date, by odometer reading, or both.</p></div>
      )}
      <p className="small muted" style={{ marginTop: 10 }}>Document expiries are reminded automatically; see the Documents tab.</p>
      <div className="actions"><button className="btn" onClick={() => openSheet({ type: 'rem', rem: null })}>Add reminder</button></div>
    </>
  )
}

function ReminderSheet({ car, rem, onClose, onSaved }) {
  const editing = !!rem
  const [f, setF] = useState({ title: rem?.title || '', due_on: rem?.due_on || '', due_km: rem?.due_km ?? '', repeat: rem?.repeat || 'none',
    every_km: rem?.every_km ?? '', notes: rem?.notes || '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const set = (k) => (e) => setF({ ...f, [k]: e.target ? e.target.value : e })
  async function save(e) {
    e.preventDefault()
    setBusy(true); setError('')
    const hasKm = f.due_km !== ''
    const body = { ...f, due_on: f.due_on || null, due_km: hasKm ? f.due_km : null, every_km: hasKm && f.every_km !== '' ? f.every_km : null }
    try {
      await api(editing ? `/reminders/${rem.id}` : `/cars/${car.id}/reminders`, { method: editing ? 'PATCH' : 'POST', body })
      onSaved()
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  async function remove() {
    try { await api(`/reminders/${rem.id}`, { method: 'DELETE' }); onSaved() } catch (err) { setError(err.message) }
  }
  return (
    <Sheet title={editing ? 'Reminder' : 'Add a reminder'} onClose={onClose}>
      <form onSubmit={save}>
        {!editing && (
          <div className="segment" role="group" aria-label="Suggestions">
            {IDEAS.map((t) => <button type="button" key={t} aria-pressed={f.title === t} onClick={() => setF({ ...f, title: t })}>{t}</button>)}
          </div>
        )}
        <Field label="What"><input required minLength={2} maxLength={80} value={f.title} onChange={set('title')} /></Field>
        <div className="two">
          <Field label="Due on"><input type="date" value={f.due_on} onChange={set('due_on')} /></Field>
          <Field label="Or at (km)" hint={car.odometer_km != null ? `Now ${km(car.odometer_km)}` : ''}><input inputMode="numeric" value={f.due_km} onChange={set('due_km')} /></Field>
        </div>
        <span className="field"><span>Repeat</span></span>
        <Segment label="Repeat" options={REPEAT} value={f.repeat} onChange={set('repeat')} />
        {f.due_km !== '' && (
          <Field label="And every (km)" hint="For a service every 10,000 km, enter 10000. Marking it done moves it to the next one.">
            <input inputMode="numeric" value={f.every_km} onChange={set('every_km')} placeholder="Leave empty for once" />
          </Field>
        )}
        <Field label="Notes"><textarea maxLength={300} value={f.notes} onChange={set('notes')} /></Field>
        <ErrorText error={error} />
        <div className="actions">
          <button className="btn" disabled={busy}>{editing ? 'Save changes' : 'Add reminder'}</button>
          {editing && <button type="button" className="btn quiet" onClick={remove}>Delete</button>}
        </div>
      </form>
    </Sheet>
  )
}

// ---------------------------------------------------------------- people
function People({ car, openSheet }) {
  const { user } = useAuth()
  const nav = useNavigate()
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const load = useCallback(() => { api(`/cars/${car.id}/members`).then(setData).catch((e) => setError(e.message)) }, [car.id])
  useReload(load)
  const owner = car.role === 'owner'
  async function remove(m) {
    const self = m.user_id === user.id
    if (!window.confirm(self ? 'Leave this car? It disappears from your garage.' : `Remove ${m.name || m.email}?`)) return
    try {
      await api(`/cars/${car.id}/members/${m.user_id}`, { method: 'DELETE' })
      self ? nav('/garage') : load()
    } catch (err) { setError(err.message) }
  }
  async function revoke(p) {
    try { await api(`/cars/${car.id}/invitations/${p.id}`, { method: 'DELETE' }); load() } catch (err) { setError(err.message) }
  }
  if (!data) return error ? <p className="error">{error}</p> : <p className="muted">Loading…</p>
  return (
    <>
      <ErrorText error={error} />
      <div className="list">
        {data.members.map((m) => (
          <div key={m.user_id} className="row">
            <div className="grow"><div className="title">{m.name || m.email}{m.you ? ' (you)' : ''}</div><div className="meta">{m.email}</div></div>
            <span className="role">{m.role === 'owner' ? 'Owner' : 'Driver'}</span>
            {(owner || m.you) && data.members.length > 1 && <button className="linkbtn small" onClick={() => remove(m)}>{m.you ? 'Leave' : 'Remove'}</button>}
          </div>
        ))}
        {data.pending.map((p) => (
          <div key={p.id} className="row">
            <div className="grow"><div className="title">{p.email}</div><div className="meta">Invited as {p.role}, waiting for them to accept</div></div>
            {owner && <button className="linkbtn small" onClick={() => revoke(p)}>Withdraw</button>}
          </div>
        ))}
      </div>
      <p className="small muted" style={{ marginTop: 10 }}>Drivers see everything and can add documents, reminders and the odometer reading. Owners can also edit, share and remove the car.</p>
      {owner && <div className="actions"><button className="btn" onClick={() => openSheet({ type: 'invite' })}>Share this car</button></div>}
    </>
  )
}

function InviteSheet({ car, onClose, onSaved }) {
  const [email, setEmail] = useState('')
  const [role, setRole] = useState('driver')
  const [error, setError] = useState('')
  async function save(e) {
    e.preventDefault()
    try { await api(`/cars/${car.id}/members`, { method: 'POST', body: { email, role } }); onSaved() } catch (err) { setError(err.message) }
  }
  return (
    <Sheet title="Share this car" onClose={onClose}>
      <form onSubmit={save}>
        <Field label="Their email" hint="They see the invitation after signing in with this address.">
          <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        <span className="field"><span>Access</span></span>
        <Segment label="Access" options={{ driver: 'Driver', owner: 'Co-owner' }} value={role} onChange={setRole} />
        <ErrorText error={error} />
        <div className="actions"><button className="btn">Send invitation</button></div>
      </form>
    </Sheet>
  )
}

// ---------------------------------------------------------------- details, odometer, removal
function Details({ car, owner, openSheet }) {
  const rows = [['Registration', car.reg_display], ['Make and model', `${car.make} ${car.model}`], ['Variant', car.variant], ['Fuel', FUELS[car.fuel]],
    ['Year', car.year], ['Colour', car.colour], ['Bought on', niceDate(car.purchase_date)], ['VIN', car.vin], ['Nickname', car.nickname],
    ['Odometer', car.odometer_km != null ? `${km(car.odometer_km)} on ${niceDate(car.odometer_at)}` : ''],
    ['Roadside assistance', car.roadside_phone ? <a href={`tel:${car.roadside_phone.replace(/[^\d+]/g, '')}`}>{car.roadside_phone}</a> : ''],
    ['Your access', owner ? 'Owner' : 'Driver']]
  return (
    <>
      <dl className="facts">{rows.filter(([, v]) => v).map(([k, v]) => <Fragment key={k}><dt>{k}</dt><dd>{v}</dd></Fragment>)}</dl>
      {owner && (
        <div className="actions">
          <Link className="btn quiet" to={`/cars/${car.id}/edit`}>Edit car</Link>
          <button className="btn quiet" onClick={() => openSheet({ type: 'remove' })}>Remove car</button>
        </div>
      )}
    </>
  )
}

function OdometerSheet({ car, onClose, onSaved }) {
  const [v, setV] = useState(car.odometer_km ?? '')
  const [error, setError] = useState('')
  async function save(e) {
    e.preventDefault()
    try { onSaved(await api(`/cars/${car.id}`, { method: 'PATCH', body: { odometer_km: v } })) } catch (err) { setError(err.message) }
  }
  return (
    <Sheet title="Odometer reading" onClose={onClose}>
      <form onSubmit={save}>
        <Field label="Kilometres on the odometer" hint="Reminders by km use this reading">
          <input inputMode="numeric" required value={v} onChange={(e) => setV(e.target.value.replace(/\D/g, ''))} />
        </Field>
        <ErrorText error={error} />
        <div className="actions"><button className="btn">Save reading</button></div>
      </form>
    </Sheet>
  )
}

function RemoveSheet({ car, onClose }) {
  const nav = useNavigate()
  const [error, setError] = useState('')
  async function remove() {
    try { await api(`/cars/${car.id}`, { method: 'DELETE' }); nav('/garage', { replace: true }) } catch (err) { setError(err.message) }
  }
  return (
    <Sheet title="Remove this car?" onClose={onClose}>
      <p>{car.make} {car.model} ({car.reg_display}) is removed with all its documents and reminders, for everyone it is shared with. This cannot be undone.</p>
      <ErrorText error={error} />
      <div className="actions"><button className="btn danger" onClick={remove}>Remove car</button><button className="btn quiet" onClick={onClose}>Keep it</button></div>
    </Sheet>
  )
}
