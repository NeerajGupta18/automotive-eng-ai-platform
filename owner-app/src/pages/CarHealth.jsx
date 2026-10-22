import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { ErrorText } from '../components/Field.jsx'
import Sheet from '../components/Sheet.jsx'
import { bleLink, bluetoothAvailable, demoLink, Elm327 } from '../lib/obd.js'

const MARK = { red: '!', amber: '!', green: '✓', info: 'i' }
const SOURCE = { adapter: 'OBD-II adapter', demo: 'demo adapter', manual: 'codes typed in' }
const DRIVE = { no: 'Don’t drive', carefully: 'Get it checked soon', yes: 'OK to drive' }

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
function when(iso) { // the server keeps UTC; show the phone's local time
  if (!iso) return ''
  const d = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`)
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}, ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

export default function CarHealth({ car, goTab }) {
  const [h, setH] = useState(null)
  const [scans, setScans] = useState([])
  const [sheet, setSheet] = useState(null)
  const [error, setError] = useState('')
  const load = useCallback(() => {
    api(`/cars/${car.id}/health`).then(setH).catch((e) => setError(e.message))
    api(`/cars/${car.id}/health/scans`).then(setScans).catch(() => {})
  }, [car.id])
  useEffect(() => { load() }, [load])
  if (error) return <p className="error">{error}</p>
  if (!h) return <p className="muted">Loading…</p>
  const s = h.last_scan

  function act(f) {
    const a = f.action
    if (!a) return null
    if (a.type === 'code') {
      return (
        <span className="linkrow">
          <button className="linkbtn small" onClick={() => setSheet({ type: 'code', code: a.target })}>What this means</button>
          {f.level !== 'info' && <Link className="linkbtn small" to={`/cars/${car.id}/book?code=${a.target}`}>Book a check</Link>}
        </span>
      )
    }
    if (a.type === 'guide') return <Link className="linkbtn small" to={`/help/guide/${a.target}?car=${car.id}`}>Step-by-step help</Link>
    if (a.type === 'light') return <Link className="linkbtn small" to={`/help/lights?light=${a.target}&car=${car.id}`}>About this light</Link>
    if (a.type === 'scan') return <button className="linkbtn small" onClick={() => setSheet({ type: 'scan' })}>Check now</button>
    const tabs = { reminders: 'Reminders', documents: 'Documents', costs: 'Costs' }
    return tabs[a.type] ? <button className="linkbtn small" onClick={() => goTab(a.type)}>Open {tabs[a.type]}</button> : null
  }

  return (
    <>
      <section className={`health ${h.status}`} aria-labelledby="health-label">
        <span className="badge" aria-hidden="true">{MARK[h.status]}</span>
        <div>
          <h2 id="health-label">{h.label}</h2>
          <p className="small">
            {s ? `Last check ${when(s.scanned_at)} (${SOURCE[s.source]})` : 'No check from the car yet'}
            {h.km_per_day ? `. Driven about ${h.km_per_day} km a day.` : '.'}
          </p>
        </div>
      </section>

      <div className="list findings">
        {h.findings.map((f, i) => (
          <div key={i} className={`row finding ${f.level}`}>
            <div className="grow">
              <div className="title">{f.title}</div>
              {f.detail && <div className="meta">{f.detail}</div>}
              {act(f)}
            </div>
          </div>
        ))}
        {h.findings.length === 0 && <div className="row"><div className="grow"><div className="title">Nothing to report</div></div></div>}
      </div>

      <div className="actions">
        <button className="btn" onClick={() => setSheet({ type: 'scan' })}>Check with an OBD adapter</button>
        <button className="btn quiet" onClick={() => setSheet({ type: 'codes' })}>Enter fault codes</button>
      </div>

      {s && s.source !== 'manual' && (
        <>
          <h2>Last reading</h2>
          <dl className="facts">
            {[['Engine light', s.mil_on == null ? '' : s.mil_on ? 'On' : 'Off'],
              ['Battery', s.battery_v ? `${s.battery_v.toFixed(1)} V${s.engine_running ? ' (engine running)' : s.engine_running === false ? ' (engine off)' : ''}` : ''],
              ['Coolant', s.coolant_c != null ? `${s.coolant_c} °C` : ''], ['Engine speed', s.rpm != null ? `${s.rpm} rpm` : ''],
              ['Driven with the light on', s.km_with_mil != null ? `${s.km_with_mil} km` : ''],
              ['Since codes were cleared', s.km_since_clear != null ? `${s.km_since_clear.toLocaleString('en-IN')} km` : ''],
              ['Adapter', [s.adapter, s.protocol].filter(Boolean).join(', ')]]
              .filter(([, v]) => v).map(([k, v]) => <div key={k} style={{ display: 'contents' }}><dt>{k}</dt><dd>{v}</dd></div>)}
          </dl>
        </>
      )}

      {scans.length > 1 && (
        <>
          <h2>Earlier checks</h2>
          <div className="list">
            {scans.slice(1, 8).map((x) => (
              <div key={x.id} className="row">
                <div className="grow">
                  <div className="title">{x.codes.length ? x.codes.map((c) => c.code).join(', ') : 'No fault codes'}</div>
                  <div className="meta">{when(x.scanned_at)}, {SOURCE[x.source]}{x.battery_v ? `, battery ${x.battery_v.toFixed(1)} V` : ''}</div>
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      {sheet?.type === 'scan' && <ScanSheet car={car} onClose={() => setSheet(null)} onDone={() => { setSheet(null); load() }} />}
      {sheet?.type === 'codes' && <CodesSheet car={car} onClose={() => setSheet(null)} onDone={() => { setSheet(null); load() }} />}
      {sheet?.type === 'code' && <CodeSheet code={sheet.code} onClose={() => setSheet(null)} />}
    </>
  )
}

function ScanSheet({ car, onClose, onDone }) {
  const [phase, setPhase] = useState('intro')
  const [step, setStep] = useState('')
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  const link = useRef(null)
  const ble = bluetoothAvailable()
  useEffect(() => () => { link.current?.close?.() }, [])

  async function run(kind) {
    setError(''); setPhase('running')
    try {
      setStep(kind === 'demo' ? 'Connecting to the demo adapter' : 'Choose your adapter in the list')
      link.current = kind === 'demo' ? demoLink() : await bleLink(() => setError('The adapter disconnected.'))
      const { commands } = await api('/obd/commands')
      const elm = new Elm327(link.current)
      setStep('Setting up the adapter')
      await elm.init()
      const answers = await elm.scan(commands, (c) => setStep(`Reading from the car (${commands.indexOf(c) + 1} of ${commands.length})`))
      setStep('Working out what it means')
      const r = await api(`/cars/${car.id}/health/scans`, { method: 'POST', body: { source: kind, answers } })
      setResult(r); setPhase('done')
    } catch (err) {
      setError(err?.message?.includes('cancel') ? 'No adapter chosen.' : err.message || 'The check did not finish.')
      setPhase('intro')
    } finally {
      await link.current?.close?.()
      link.current = null
    }
  }

  return (
    <Sheet title="Check with an OBD adapter" onClose={onClose}>
      {phase === 'intro' && (
        <>
          <ol className="todo">
            <li>Plug a Bluetooth LE OBD-II adapter (ELM327, about ₹500 to ₹1,500) into the port under the dashboard, near the steering column.</li>
            <li>Switch the ignition on, or start the engine for a battery charging check.</li>
            <li>Turn on Bluetooth on the phone and tap Connect. Keep the phone in the car until the check finishes.</li>
          </ol>
          {!ble && <p className="notice small">This browser can't use Bluetooth. Use the CarSaathi Android app or Chrome, or enter the codes a workshop read for you.</p>}
          <ErrorText error={error} />
          <div className="answers">
            <button className="btn block" disabled={!ble} onClick={() => run('adapter')}>Connect adapter</button>
            <button className="btn quiet block" onClick={() => run('demo')}>Try the demo adapter</button>
          </div>
          <p className="small muted" style={{ marginTop: 10 }}>Reading codes doesn't change anything in the car. CarSaathi never clears codes or changes settings.</p>
        </>
      )}
      {phase === 'running' && (
        <div className="scanning" role="status" aria-live="polite">
          <span className="spinner" aria-hidden="true" />
          <p>{step}…</p>
        </div>
      )}
      {phase === 'done' && result && (
        <>
          <p className="step-text">{result.health.label}</p>
          <p className="small">
            {result.scan.codes.length ? `${result.scan.codes.length} fault code${result.scan.codes.length > 1 ? 's' : ''}: ${result.scan.codes.map((c) => c.code).join(', ')}.` : 'No fault codes.'}
            {result.scan.battery_v ? ` Battery ${result.scan.battery_v.toFixed(1)} V.` : ''}
          </p>
          <div className="answers"><button className="btn block" onClick={onDone}>See the details</button></div>
        </>
      )}
    </Sheet>
  )
}

function CodesSheet({ car, onClose, onDone }) {
  const [text, setText] = useState('')
  const [mil, setMil] = useState(true)
  const [info, setInfo] = useState({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const codes = [...new Set(text.toUpperCase().split(/[\s,;]+/).filter((c) => /^[PCBU][0-3][0-9A-F]{3}$/.test(c)))]
  const key = codes.join(',')
  useEffect(() => {
    key.split(',').filter((c) => c && !info[c]).forEach((c) => {
      api(`/obd/codes/${c}`).then((d) => setInfo((m) => ({ ...m, [c]: d }))).catch(() => {})
    })
  }, [key]) // eslint-disable-line react-hooks/exhaustive-deps
  async function save(e) {
    e.preventDefault()
    setBusy(true); setError('')
    try { await api(`/cars/${car.id}/health/codes`, { method: 'POST', body: { codes, mil_on: mil } }); onDone() }
    catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  return (
    <Sheet title="Enter fault codes" onClose={onClose}>
      <form onSubmit={save}>
        <label className="field">
          <span>Codes from a workshop's scan or a scanner app</span>
          <input autoCapitalize="characters" value={text} onChange={(e) => setText(e.target.value)} placeholder="P0171 P0420" />
          <small className="hint">A letter (P, C, B or U) and four characters, separated by spaces</small>
        </label>
        <label className="check"><input type="checkbox" checked={mil} onChange={(e) => setMil(e.target.checked)} /><span>The engine warning light is on</span></label>
        {codes.map((c) => info[c] && (
          <div key={c} className={`codecard ${info[c].severity}`}>
            <strong>{c}: {info[c].title}</strong>
            <span className="small">{DRIVE[info[c].drive]}</span>
          </div>
        ))}
        <ErrorText error={error} />
        <div className="actions"><button className="btn" disabled={busy || !codes.length}>{busy ? 'Saving…' : `Save ${codes.length || ''} code${codes.length === 1 ? '' : 's'}`}</button></div>
      </form>
    </Sheet>
  )
}

function CodeSheet({ code, onClose }) {
  const [d, setD] = useState(null)
  useEffect(() => { api(`/obd/codes/${code}`).then(setD).catch(() => {}) }, [code])
  return (
    <Sheet title={code} onClose={onClose}>
      {!d ? <p className="muted">Loading…</p> : (
        <>
          <h3 style={{ marginBottom: 6 }}>{d.title}</h3>
          <p className={`sev ${d.severity}`}>{DRIVE[d.drive]}</p>
          <p>{d.plain}</p>
          {d.causes.length > 0 && (<><h3 style={{ margin: '12px 0 6px' }}>Common causes</h3><ul className="tell">{d.causes.map((c) => <li key={c}>{c}</li>)}</ul></>)}
          <p className="small muted" style={{ marginTop: 12 }}>{d.drive_text} Fault codes point to where to look; a workshop confirms the cause before replacing parts.</p>
          {d.guide && <div className="actions"><Link className="btn" to={`/help/guide/${d.guide}`}>Step-by-step help</Link></div>}
        </>
      )}
    </Sheet>
  )
}
