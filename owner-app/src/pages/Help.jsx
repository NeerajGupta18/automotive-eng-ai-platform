import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import Field, { ErrorText } from '../components/Field.jsx'
import { TopicIcon } from '../components/Icons.jsx'
import LightIcon from '../components/LightIcon.jsx'
import Sheet from '../components/Sheet.jsx'
import Urgent, { Calls } from '../components/Urgent.jsx'
import { niceDate } from '../format.js'

// The car the help is for: remembered per viewer so the choice survives a reload (a convenience only).
function useHelpCar(cars) {
  const [params, setParams] = useSearchParams()
  let saved = null
  try { saved = localStorage.getItem('cs_help_car') } catch { /* private mode */ }
  const wanted = params.get('car') || saved
  const car = cars?.find((c) => c.id === wanted) || cars?.[0] || null
  function choose(id) {
    try { localStorage.setItem('cs_help_car', id) } catch { /* ignore */ }
    const p = new URLSearchParams(params); p.set('car', id); setParams(p, { replace: true })
  }
  return [car, choose]
}

export function CarPicker({ cars, car, onChange }) {
  if (!cars || cars.length < 2) return null
  return (
    <div className="segment" role="group" aria-label="Which car">
      {cars.map((c) => (
        <button type="button" key={c.id} aria-pressed={car?.id === c.id} onClick={() => onChange(c.id)}>
          {c.nickname || `${c.make} ${c.model}`}
        </button>
      ))}
    </div>
  )
}

function ResultRow({ g, best, carId }) {
  return (
    <Link className={`row guide${best ? ' best' : ''}`} to={`/help/guide/${g.id}${carId ? `?car=${carId}` : ''}`}>
      <div className="grow">
        {best && <div className="tag">Best match</div>}
        <div className="title">{g.title}</div>
        <div className="meta">{g.summary}</div>
      </div>
    </Link>
  )
}

export default function Help() {
  const nav = useNavigate()
  const [cars, setCars] = useState(null)
  const [topics, setTopics] = useState(null)
  const [requests, setRequests] = useState([])
  const [q, setQ] = useState('')
  const [res, setRes] = useState(null)
  const [safe, setSafe] = useState(false)
  const [open, setOpen] = useState(null)
  const [sheet, setSheet] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [car, chooseCar] = useHelpCar(cars)

  useEffect(() => {
    api('/cars').then(setCars).catch((e) => setError(e.message))
    api('/help/requests').then(setRequests).catch(() => {})
  }, [])
  useEffect(() => {
    if (cars === null) return
    api(`/help/topics${car ? `?car_id=${car.id}` : ''}`).then(setTopics).catch((e) => setError(e.message))
  }, [cars, car?.id])

  async function search(e) {
    e.preventDefault()
    if (q.trim().length < 2) return
    setBusy(true); setError(''); setSafe(false)
    try {
      setRes(await api('/help/search', { method: 'POST', body: { q: q.trim(), car_id: car?.id || null } }))
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  const stopped = res?.urgent?.level === 'stop' && !safe
  const openRequests = requests.filter((r) => r.status === 'open')

  return (
    <>
      <p className="greeting">What's wrong?</p>
      <p className="sub">Describe it in your own words, in English or Hinglish. Step-by-step fixes for the common problems.</p>
      <CarPicker cars={cars} car={car} onChange={chooseCar} />
      <form className="searchbar" onSubmit={search} role="search">
        <input aria-label="Describe the problem" value={q} onChange={(e) => setQ(e.target.value)} maxLength={500}
          placeholder="e.g. Bluetooth won't connect" enterKeyHint="search" />
        <button className="btn" disabled={busy || q.trim().length < 2}>{busy ? 'Finding…' : 'Find help'}</button>
      </form>
      <ErrorText error={error} />

      {res && (
        <section aria-live="polite">
          {res.urgent && <Urgent card={res.urgent} onSafe={() => setSafe(true)} />}
          {!stopped && (
            <>
              {res.results.length > 0 && (
                <>
                  <h2>{res.confident ? 'This should help' : 'Not sure this is it'}</h2>
                  {!res.confident && <p className="small muted">The words didn't match a guide clearly. Check these, pick a topic below, or describe it to your service centre.</p>}
                  <div className="list">
                    {res.results.slice(0, res.confident ? 3 : 4).map((g, i) => <ResultRow key={g.id} g={g} best={res.confident && i === 0} carId={car?.id} />)}
                  </div>
                </>
              )}
              {res.lights.length > 0 && (
                <>
                  <h2>Warning lights that match</h2>
                  <div className="list">
                    {res.lights.map((l) => (
                      <Link key={l.id} className="row" to={`/help/lights?light=${l.id}${car ? `&car=${car.id}` : ''}`}>
                        <span className={`lamp ${l.color}`}><LightIcon id={l.id} size={30} /></span>
                        <div className="grow"><div className="title">{l.name}</div><div className="meta">{l.meaning}</div></div>
                      </Link>
                    ))}
                  </div>
                </>
              )}
              <div className="actions">
                <button className="btn quiet small" onClick={() => setSheet({ type: 'request', title: q.trim() })}>None of these: ask for help</button>
              </div>
            </>
          )}
        </section>
      )}

      {openRequests.length > 0 && (
        <>
          <h2>Your open help requests</h2>
          <div className="list">
            {openRequests.map((r) => (
              <Link key={r.id} className="row" to={`/help/requests/${r.id}`}>
                <div className="grow">
                  <div className="title">{r.title}</div>
                  <div className="meta">{[r.ref, r.car_label, niceDate(r.created_at)].filter(Boolean).join(', ')}</div>
                </div>
                {r.report_status && <span className="role">Reported</span>}
              </Link>
            ))}
          </div>
        </>
      )}

      <h2>Browse by topic</h2>
      {!topics ? <p className="muted">Loading…</p> : (
        <div className="list">
          {topics.topics.map((t) => (
            <div key={t.id} className="topic">
              <button className="row" aria-expanded={open === t.id} onClick={() => setOpen(open === t.id ? null : t.id)}>
                <span className="ticon"><TopicIcon name={t.icon} /></span>
                <span className="grow title">{t.title}</span>
                <span className="muted small">{t.guides.length}</span>
              </button>
              {open === t.id && (
                <div className="sublist">
                  {t.guides.map((g) => (
                    <Link key={g.id} to={`/help/guide/${g.id}${car ? `?car=${car.id}` : ''}`}>{g.title}</Link>
                  ))}
                </div>
              )}
            </div>
          ))}
          <Link className="row" to={`/help/lights${car ? `?car=${car.id}` : ''}`}>
            <span className="ticon"><TopicIcon name="light" /></span>
            <span className="grow title">Dashboard warning lights</span>
            <span className="muted small">{topics.lights}</span>
          </Link>
        </div>
      )}

      <div className="emergency">
        <div>
          <h3>Breakdown or accident?</h3>
          <p className="small">Stop where it's safe, switch on the hazard lights, keep everyone away from traffic.</p>
        </div>
        {topics && <Calls calls={topics.calls} />}
      </div>
      {requests.length > openRequests.length && (
        <p className="small muted" style={{ marginTop: 14 }}>
          {requests.length - openRequests.length} resolved request(s): <Link to="/help/requests">see all</Link>
        </p>
      )}
      {sheet?.type === 'request' && (
        <RequestSheet cars={cars} car={car} title={sheet.title} onClose={() => setSheet(null)} onSaved={(c) => nav(`/help/requests/${c.id}`)} />
      )}
    </>
  )
}

export function RequestSheet({ cars, car, title, guideId, transcript, onClose, onSaved }) {
  const [f, setF] = useState({ car_id: car?.id || '', title: title || '', description: '', happened_on: new Date().toISOString().slice(0, 10) })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })
  async function save(e) {
    e.preventDefault()
    setBusy(true); setError('')
    const body = { ...f, car_id: f.car_id || null, guide_id: guideId || null, transcript: transcript || null }
    if (guideId) delete body.title
    try { onSaved(await api('/help/requests', { method: 'POST', body })) } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  return (
    <Sheet title="Ask for help" onClose={onClose}>
      <form onSubmit={save}>
        <p className="small muted">Saved in your account with {guideId ? 'the steps you tried' : 'your description'}, ready to send to your service centre.</p>
        {cars?.length > 0 && (
          <Field label="Car">
            <select value={f.car_id} onChange={set('car_id')}>
              {cars.map((c) => <option key={c.id} value={c.id}>{c.make} {c.model}, {c.reg_display}</option>)}
              <option value="">Not about one of my cars</option>
            </select>
          </Field>
        )}
        {!guideId && <Field label="The problem in a few words"><input required minLength={3} maxLength={120} value={f.title} onChange={set('title')} /></Field>}
        <Field label="What happened" hint="When it happens, what you see or hear, anything that changed recently">
          <textarea maxLength={2000} value={f.description} onChange={set('description')} />
        </Field>
        <Field label="When did you notice it?"><input type="date" max={new Date().toISOString().slice(0, 10)} value={f.happened_on} onChange={set('happened_on')} /></Field>
        <ErrorText error={error} />
        <div className="actions"><button className="btn" disabled={busy}>{busy ? 'Saving…' : 'Save help request'}</button></div>
      </form>
    </Sheet>
  )
}

export function AllRequests() {
  const [rows, setRows] = useState(null)
  useEffect(() => { api('/help/requests').then(setRows).catch(() => setRows([])) }, [])
  if (!rows) return <p className="muted">Loading…</p>
  if (!rows.length) return <div className="empty"><h3>No help requests</h3><p>When a guide doesn't fix a problem, you can save it as a request for your service centre.</p></div>
  return (
    <div className="list">
      {rows.map((r) => (
        <Link key={r.id} className={`row ${r.status === 'open' ? 'due_soon' : 'done'}`} to={`/help/requests/${r.id}`}>
          <div className="grow"><div className="title">{r.title}</div><div className="meta">{[r.ref, r.car_label, niceDate(r.created_at)].filter(Boolean).join(', ')}</div></div>
          <span className="small muted">{r.status === 'open' ? 'Open' : 'Resolved'}</span>
        </Link>
      ))}
    </div>
  )
}
