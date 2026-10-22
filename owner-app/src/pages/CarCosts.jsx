import { useCallback, useEffect, useState } from 'react'
import { api, download } from '../api.js'
import { inr, MileageLine, MonthlyBars } from '../components/Charts.jsx'
import Field, { ErrorText, Segment } from '../components/Field.jsx'
import Sheet from '../components/Sheet.jsx'
import { km, niceDate } from '../format.js'

const CATEGORIES = { service: 'Service', repair: 'Repair', tyres: 'Tyres', battery: 'Battery', insurance: 'Insurance', puc: 'PUC',
  parking: 'Parking', toll: 'Toll', fastag: 'FASTag', fine: 'Challan', wash: 'Cleaning', accessories: 'Accessories', other: 'Other' }
const UNITS = { petrol: ['L'], diesel: ['L'], hybrid: ['L'], cng: ['kg', 'L'], electric: ['kWh'] }
const today = () => new Date().toISOString().slice(0, 10)

function Tile({ label, value, sub }) {
  return (
    <div className="tile">
      <span className="tile-label">{label}</span>
      <strong className="tile-value">{value}</strong>
      {sub && <span className="tile-sub">{sub}</span>}
    </div>
  )
}

export default function CarCosts({ car, onCarChanged }) {
  const [sum, setSum] = useState(null)
  const [entries, setEntries] = useState(null)
  const [sheet, setSheet] = useState(null)
  const [all, setAll] = useState(false)
  const [error, setError] = useState('')
  const ev = car.fuel === 'electric'
  const load = useCallback(() => {
    Promise.all([api(`/cars/${car.id}/costs`), api(`/cars/${car.id}/fuel`), api(`/cars/${car.id}/expenses`)])
      .then(([s, f, x]) => {
        setSum(s)
        setEntries([...f.map((e) => ({ ...e, kind: 'fuel', on: e.filled_on })), ...x.map((e) => ({ ...e, kind: 'expense', on: e.spent_on }))]
          .sort((a, b) => b.on.localeCompare(a.on) || b.created_at.localeCompare(a.created_at)))
      }).catch((e) => setError(e.message))
  }, [car.id])
  useEffect(() => { load() }, [load])
  if (error) return <p className="error">{error}</p>
  if (!sum) return <p className="muted">Loading…</p>
  const units = Object.values(sum.mileage)
  const m = units[0]
  const saved = () => { setSheet(null); load(); onCarChanged?.() }
  const shown = all ? entries : entries.slice(0, 8)

  if (!sum.fills && !sum.expenses) {
    return (
      <>
        <div className="empty">
          <h3>Track what the car costs</h3>
          <p>Log each {ev ? 'charge' : 'fill'} with the odometer reading and CarSaathi works out your real mileage, cost per km and when
            km-based services will fall due. Add service bills, tolls and parking too.</p>
          <div className="actions">
            <button className="btn" onClick={() => setSheet({ type: 'fuel' })}>{ev ? 'Add a charge' : 'Add a fill'}</button>
            <button className="btn quiet" onClick={() => setSheet({ type: 'expense' })}>Add an expense</button>
          </div>
        </div>
        {sheet?.type === 'fuel' && <FuelSheet car={car} onClose={() => setSheet(null)} onSaved={saved} />}
        {sheet?.type === 'expense' && <ExpenseSheet car={car} onClose={() => setSheet(null)} onSaved={saved} />}
      </>
    )
  }

  return (
    <>
      <div className="tiles">
        <Tile label="This month" value={inr(sum.this_month)} />
        <Tile label={`Last ${sum.period_months} months`} value={inr(sum.period_total)} sub={sum.km_driven ? `${km(sum.km_driven)} driven` : null} />
        <Tile label="Cost per km" value={sum.cost_per_km ? inr(sum.cost_per_km, 2) : '–'} sub={sum.cost_per_km ? 'fuel and expenses' : 'needs 300 km of history'} />
        <Tile label="Mileage" value={m?.latest ? `${m.latest} ${m.label}` : '–'} sub={m?.average ? `average ${m.average}` : `needs two ${ev ? 'charges to the limit' : 'full fills'}`} />
      </div>
      {units.filter((u) => u.check).map((u) => (
        <p key={u.unit} className="notice warn small">
          <strong>Mileage dropped {u.check.drop_pct}%</strong>: {u.check.latest} {u.label} on the last {ev ? 'charge' : 'fill'}, usually {u.check.usual}.
          Check tyre pressures and the air filter; if it stays low for two more {ev ? 'charges' : 'fills'}, get the car checked.
        </p>
      ))}
      {sum.km_per_day && <p className="small muted">You drive about {sum.km_per_day} km a day; km-based reminders show the date that works out to.</p>}
      <div className="actions" style={{ marginTop: 6 }}>
        <button className="btn" onClick={() => setSheet({ type: 'fuel' })}>{ev ? 'Add a charge' : 'Add a fill'}</button>
        <button className="btn quiet" onClick={() => setSheet({ type: 'expense' })}>Add an expense</button>
      </div>

      <MonthlyBars months={sum.months} />
      {units.filter((u) => u.series.length >= 2).map((u) => <MileageLine key={u.unit} series={u.series} unit={u.unit} usual={u.check ? u.check.usual : null} />)}

      {sum.by_category.length > 0 && (
        <>
          <h2>Other costs, last {sum.period_months} months</h2>
          <div className="catlist">
            {sum.by_category.map((c) => (
              <div key={c.category} className="cat">
                <span>{c.label}</span><strong>{inr(c.amount)}</strong>
                <i style={{ width: `${(100 * c.amount) / sum.by_category[0].amount}%` }} aria-hidden="true" />
              </div>
            ))}
          </div>
        </>
      )}

      <h2>Entries</h2>
      <div className="list">
        {shown.map((e) => (
          <button key={e.kind + e.id} className="row" onClick={() => setSheet({ type: 'entry', entry: e })}>
            <div className="grow">
              <div className="title">{e.kind === 'fuel' ? `${e.quantity} ${e.unit}${e.full ? '' : ' (part)'}` : e.category_label}</div>
              <div className="meta">{[niceDate(e.on), e.odometer_km != null && km(e.odometer_km), e.kind === 'fuel' ? e.station : e.vendor].filter(Boolean).join(', ')}</div>
            </div>
            <span className="amount">{inr(e.amount)}</span>
          </button>
        ))}
      </div>
      {entries.length > 8 && <p><button className="linkbtn" onClick={() => setAll(!all)}>{all ? 'Show fewer' : `Show all ${entries.length}`}</button></p>}
      <p><button className="linkbtn small" onClick={() => download(`/cars/${car.id}/costs.csv`, `${car.reg_display.replace(/ /g, '')}-costs.csv`)}>Download all entries (CSV)</button></p>

      {sheet?.type === 'fuel' && <FuelSheet car={car} onClose={() => setSheet(null)} onSaved={saved} />}
      {sheet?.type === 'expense' && <ExpenseSheet car={car} onClose={() => setSheet(null)} onSaved={saved} />}
      {sheet?.type === 'entry' && <EntrySheet entry={sheet.entry} onClose={() => setSheet(null)} onDeleted={saved} />}
    </>
  )
}

function FuelSheet({ car, onClose, onSaved }) {
  const units = UNITS[car.fuel]
  const ev = car.fuel === 'electric'
  const [f, setF] = useState({ filled_on: today(), odometer_km: car.odometer_km ?? '', quantity: '', amount: '', unit: units[0], full: true, station: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const set = (k) => (e) => setF({ ...f, [k]: e.target ? (e.target.type === 'checkbox' ? e.target.checked : e.target.value) : e })
  const price = Number(f.quantity) > 0 && Number(f.amount) > 0 ? Number(f.amount) / Number(f.quantity) : null
  async function save(e) {
    e.preventDefault()
    setBusy(true); setError('')
    try { onSaved(await api(`/cars/${car.id}/fuel`, { method: 'POST', body: f })) } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  return (
    <Sheet title={ev ? 'Add a charge' : 'Add a fill'} onClose={onClose}>
      <form onSubmit={save}>
        {units.length > 1 && <Segment label="Fuel" options={Object.fromEntries(units.map((u) => [u, u === 'kg' ? 'CNG (kg)' : 'Petrol (L)']))} value={f.unit} onChange={set('unit')} />}
        <div className="two">
          <Field label="Date"><input type="date" max={today()} value={f.filled_on} onChange={set('filled_on')} /></Field>
          <Field label="Odometer (km)"><input inputMode="numeric" required value={f.odometer_km} onChange={(e) => setF({ ...f, odometer_km: e.target.value.replace(/\D/g, '') })} /></Field>
        </div>
        <div className="two">
          <Field label={`Quantity (${f.unit})`}><input inputMode="decimal" required value={f.quantity} onChange={set('quantity')} placeholder={ev ? '18.5' : '32.5'} /></Field>
          <Field label="Amount paid (₹)" hint={price ? `₹${price.toFixed(2)} per ${f.unit}` : ''}><input inputMode="decimal" required value={f.amount} onChange={set('amount')} /></Field>
        </div>
        <label className="check">
          <input type="checkbox" checked={f.full} onChange={set('full')} />
          <span>{ev ? 'Charged to the limit' : 'Filled to full'} <span className="muted">(mileage is worked out between two of these)</span></span>
        </label>
        <Field label={ev ? 'Where (optional)' : 'Fuel station (optional)'}><input maxLength={80} value={f.station} onChange={set('station')} placeholder={ev ? 'Home' : ''} /></Field>
        <ErrorText error={error} />
        <div className="actions"><button className="btn" disabled={busy}>{busy ? 'Saving…' : 'Save'}</button></div>
      </form>
    </Sheet>
  )
}

function ExpenseSheet({ car, onClose, onSaved }) {
  const [f, setF] = useState({ category: 'service', amount: '', spent_on: today(), odometer_km: '', vendor: '', notes: '' })
  const [file, setFile] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const set = (k) => (e) => setF({ ...f, [k]: e.target ? e.target.value : e })
  async function save(e) {
    e.preventDefault()
    setBusy(true); setError('')
    const form = new FormData()
    Object.entries(f).forEach(([k, v]) => form.append(k, v))
    if (file) form.append('file', file)
    try { onSaved(await api(`/cars/${car.id}/expenses`, { method: 'POST', form })) } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  return (
    <Sheet title="Add an expense" onClose={onClose}>
      <form onSubmit={save}>
        <Segment label="Category" options={CATEGORIES} value={f.category} onChange={set('category')} />
        <div className="two">
          <Field label="Amount (₹)"><input inputMode="decimal" required value={f.amount} onChange={set('amount')} /></Field>
          <Field label="Date"><input type="date" max={today()} value={f.spent_on} onChange={set('spent_on')} /></Field>
        </div>
        <div className="two">
          <Field label="Odometer (optional)"><input inputMode="numeric" value={f.odometer_km} onChange={(e) => setF({ ...f, odometer_km: e.target.value.replace(/\D/g, '') })} /></Field>
          <Field label="Paid to (optional)"><input maxLength={80} value={f.vendor} onChange={set('vendor')} /></Field>
        </div>
        <Field label="Notes (optional)"><input maxLength={300} value={f.notes} onChange={set('notes')} /></Field>
        <Field label="Bill or receipt (optional)" hint="A photo or PDF, up to 5 MB">
          <input type="file" accept="image/*,application/pdf" capture="environment" onChange={(e) => setFile(e.target.files?.[0] || null)} />
        </Field>
        <ErrorText error={error} />
        <div className="actions"><button className="btn" disabled={busy}>{busy ? 'Saving…' : 'Save'}</button></div>
      </form>
    </Sheet>
  )
}

function EntrySheet({ entry, onClose, onDeleted }) {
  const [error, setError] = useState('')
  const fuel = entry.kind === 'fuel'
  async function remove() {
    try { await api(fuel ? `/fuel/${entry.id}` : `/expenses/${entry.id}`, { method: 'DELETE' }); onDeleted() } catch (err) { setError(err.message) }
  }
  const rows = fuel
    ? [['Date', niceDate(entry.on)], ['Odometer', km(entry.odometer_km)], ['Quantity', `${entry.quantity} ${entry.unit}${entry.full ? ', to full' : ', part fill'}`],
      ['Amount', inr(entry.amount, 2)], ['Price', `₹${entry.price} per ${entry.unit}`], ['Where', entry.station]]
    : [['Date', niceDate(entry.on)], ['Category', entry.category_label], ['Amount', inr(entry.amount, 2)], ['Odometer', entry.odometer_km != null ? km(entry.odometer_km) : ''],
      ['Paid to', entry.vendor], ['Notes', entry.notes]]
  return (
    <Sheet title={fuel ? (entry.unit === 'kWh' ? 'Charge' : 'Fill') : 'Expense'} onClose={onClose}>
      <dl className="facts">{rows.filter(([, v]) => v).map(([k, v]) => <div key={k} style={{ display: 'contents' }}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>
      {!fuel && entry.has_receipt && (
        <p style={{ marginTop: 12 }}><button className="linkbtn" onClick={() => download(`/expenses/${entry.id}/receipt`, entry.receipt_name)}>Download the receipt</button></p>
      )}
      <ErrorText error={error} />
      <div className="actions"><button className="btn quiet" onClick={remove}>Delete</button></div>
    </Sheet>
  )
}
