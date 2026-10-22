import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import LightIcon from '../components/LightIcon.jsx'
import Sheet from '../components/Sheet.jsx'
import { Calls } from '../components/Urgent.jsx'

const ORDER = { stop: 0, soon: 1, info: 2 }
const SEVERITY = { stop: 'Stop safely', soon: 'Get it checked soon', info: 'Information' }

// The warning lights, laid out like an instrument cluster: tap a symbol to see what it means and what to do.
export default function Lights() {
  const [params, setParams] = useSearchParams()
  const [lights, setLights] = useState(null)
  const [calls, setCalls] = useState([])
  const [error, setError] = useState('')
  const car = params.get('car')
  useEffect(() => {
    const q = car ? `?car_id=${car}` : ''
    api(`/help/lights${q}`).then(setLights).catch((e) => setError(e.message))
    api(`/help/topics${q}`).then((t) => setCalls(t.calls)).catch(() => {})
  }, [car])
  if (error) return <p className="error">{error}</p>
  if (!lights) return <p className="muted">Loading…</p>
  const open = lights.find((l) => l.id === params.get('light'))
  const show = (id) => { const p = new URLSearchParams(params); id ? p.set('light', id) : p.delete('light'); setParams(p, { replace: true }) }
  const sorted = [...lights].sort((a, b) => ORDER[a.severity] - ORDER[b.severity])
  return (
    <>
      <p className="sub">Tap the symbol you see. Red: stop safely. Amber: get it checked soon. Symbols differ a little between cars; your owner's manual has yours.</p>
      <div className="cluster" role="list">
        {sorted.map((l) => (
          <button key={l.id} role="listitem" className={`lamp ${l.color}`} onClick={() => show(l.id)} aria-label={`${l.name}: ${SEVERITY[l.severity]}`}>
            <LightIcon id={l.id} size={44} />
            <span>{l.name}</span>
          </button>
        ))}
      </div>
      {open && (
        <Sheet title={open.name} onClose={() => show(null)}>
          <div className="light-head">
            <span className={`lamp big ${open.color}`}><LightIcon id={open.id} size={56} /></span>
            <p className={`sev ${open.severity}`}>{SEVERITY[open.severity]}</p>
          </div>
          <p>{open.meaning}</p>
          <h3 style={{ margin: '14px 0 8px' }}>What to do</h3>
          <ol className="todo">{open.todo.map((t) => <li key={t}>{t}</li>)}</ol>
          {open.severity === 'stop' && calls.length > 0 && <Calls calls={calls} />}
          {open.guide && (
            <div className="actions"><Link className="btn" to={`/help/guide/${open.guide}${car ? `?car=${car}` : ''}`}>Step-by-step help</Link></div>
          )}
        </Sheet>
      )}
    </>
  )
}
