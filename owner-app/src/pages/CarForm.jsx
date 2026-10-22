import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { FUELS } from '../format.js'
import Field, { ErrorText, Segment } from '../components/Field.jsx'
import Plate from '../components/Plate.jsx'

const EMPTY = { reg_number: '', make: '', model: '', fuel: 'petrol', year: '', variant: '', nickname: '', odometer_km: '', purchase_date: '', colour: '', vin: '',
  roadside_phone: '' }

function plateText(s) {
  const t = (s || '').toUpperCase().replace(/[^A-Z0-9]/g, '')
  const bh = t.match(/^(\d{2})(BH)(\d{0,4})([A-Z]{0,2})$/)
  if (bh) return bh.slice(1).filter(Boolean).join(' ')
  const m = t.match(/^([A-Z]{0,2})(\d{0,2})([A-Z]{0,3})(\d{0,4})$/)
  return m ? m.slice(1).filter(Boolean).join(' ') : t
}

export default function CarForm() {
  const { id } = useParams()
  const nav = useNavigate()
  const [f, setF] = useState(EMPTY)
  const [catalog, setCatalog] = useState({ makes: [] })
  const [more, setMore] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => { api('/catalog').then(setCatalog).catch(() => {}) }, [])
  useEffect(() => {
    if (!id) return
    api(`/cars/${id}`).then((c) => {
      setF(Object.fromEntries(Object.keys(EMPTY).map((k) => [k, c[k] == null ? '' : String(c[k])])))
      setMore(true)
    }).catch((e) => setError(e.message))
  }, [id])

  const models = useMemo(() => catalog.makes.find((m) => m.make.toLowerCase() === f.make.toLowerCase())?.models || [], [catalog, f.make])
  const set = (k) => (e) => setF({ ...f, [k]: e.target ? e.target.value : e })

  async function save(e) {
    e.preventDefault()
    setBusy(true); setError('')
    const body = Object.fromEntries(Object.entries(f).filter(([k, v]) => id || v !== '' || k === 'fuel'))
    try {
      const car = await api(id ? `/cars/${id}` : '/cars', { method: id ? 'PATCH' : 'POST', body })
      nav(`/cars/${car.id}${id ? '' : '?tab=documents&new=1'}`, { replace: true })
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  return (
    <form onSubmit={save}>
      <div style={{ margin: '6px 0 18px' }}><Plate reg={plateText(f.reg_number)} size="l" /></div>
      <Field label="Registration number" hint="As written on the number plate">
        <input required autoCapitalize="characters" autoComplete="off" value={f.reg_number} onChange={set('reg_number')} placeholder="MH 12 AB 1234" />
      </Field>
      <div className="two">
        <Field label="Make">
          <input required list="makes" value={f.make} onChange={set('make')} placeholder="Tata" />
          <datalist id="makes">{catalog.makes.map((m) => <option key={m.make} value={m.make} />)}</datalist>
        </Field>
        <Field label="Model">
          <input required list="models" value={f.model} onChange={set('model')} placeholder="Nexon" />
          <datalist id="models">{models.map((m) => <option key={m} value={m} />)}</datalist>
        </Field>
      </div>
      <span className="field"><span>Fuel</span></span>
      <Segment label="Fuel" options={FUELS} value={f.fuel} onChange={set('fuel')} />
      <div className="two">
        <Field label="Year"><input inputMode="numeric" value={f.year} onChange={set('year')} placeholder="2023" /></Field>
        <Field label="Odometer (km)"><input inputMode="numeric" value={f.odometer_km} onChange={set('odometer_km')} placeholder="24310" /></Field>
      </div>
      {!more ? (
        <p><button type="button" className="linkbtn" onClick={() => setMore(true)}>Add more details (variant, VIN, roadside assistance…)</button></p>
      ) : (
        <>
          <div className="two">
            <Field label="Variant"><input value={f.variant} onChange={set('variant')} placeholder="XZ+" /></Field>
            <Field label="Nickname"><input value={f.nickname} onChange={set('nickname')} placeholder="Family car" /></Field>
          </div>
          <div className="two">
            <Field label="Colour"><input value={f.colour} onChange={set('colour')} /></Field>
            <Field label="Bought on"><input type="date" value={f.purchase_date} onChange={set('purchase_date')} /></Field>
          </div>
          <Field label="VIN (chassis number)" hint="17 characters, printed on the RC. Optional.">
            <input autoCapitalize="characters" maxLength={17} value={f.vin} onChange={set('vin')} />
          </Field>
          <Field label="Roadside assistance number" hint="From your insurance or RSA card; shown on the Help tab's emergency card">
            <input type="tel" inputMode="tel" maxLength={20} value={f.roadside_phone} onChange={set('roadside_phone')} placeholder="1800 209 1234" />
          </Field>
        </>
      )}
      <ErrorText error={error} />
      <div className="actions">
        <button className="btn" disabled={busy}>{id ? 'Save changes' : 'Add car'}</button>
        <button type="button" className="btn quiet" onClick={() => nav(-1)}>Cancel</button>
      </div>
    </form>
  )
}
