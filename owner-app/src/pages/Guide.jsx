import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import { ErrorText } from '../components/Field.jsx'
import { RequestSheet } from './Help.jsx'

// Runs a guide one step at a time. The path taken is kept as a list of answers; "Back" undoes the last one.
function following(steps, id) {
  const i = steps.findIndex((s) => s.id === id)
  return steps[i + 1]?.id || 'escalate'
}

function nextOf(steps, step, answer, option) {
  if (step.type === 'ask') return step.options[option].next || following(steps, step.id)
  if (answer === 'fixed') return 'done'
  return step.next || following(steps, step.id)
}

export default function Guide() {
  const { id } = useParams()
  const [params] = useSearchParams()
  const nav = useNavigate()
  const [g, setG] = useState(null)
  const [cars, setCars] = useState(null)
  const [path, setPath] = useState([])
  const [asking, setAsking] = useState(false)
  const [error, setError] = useState('')
  const recorded = useRef(false)

  useEffect(() => {
    api(`/help/guides/${id}`).then(setG).catch((e) => setError(e.message))
    api('/cars').then(setCars).catch(() => setCars([]))
  }, [id])

  const steps = g?.steps || []
  const last = path[path.length - 1]
  const current = !g ? null : !last ? steps[0].id : nextOf(steps, steps.find((s) => s.id === last.step), last.answer, last.option)
  const step = steps.find((s) => s.id === current)
  const ended = current === 'done' || current === 'escalate'

  useEffect(() => {
    if (!ended || recorded.current || !g) return
    recorded.current = true
    api('/help/outcome', { method: 'POST', body: { guide_id: g.id, outcome: current === 'done' ? 'fixed' : 'escalated', step_id: last?.step || null } }).catch(() => {})
  }, [ended, current, g, last])

  if (error) return <p className="error">{error}</p>
  if (!g) return <p className="muted">Loading…</p>
  const car = cars?.find((c) => c.id === params.get('car')) || cars?.[0] || null
  const answer = (a, option) => setPath([...path, { step: step.id, answer: a, ...(option != null ? { option } : {}) }])
  const back = () => { recorded.current = false; setPath(path.slice(0, -1)) }
  const done = path.filter((p) => steps.find((s) => s.id === p.step)?.type !== 'info')

  return (
    <>
      <h1 className="guide-title">{g.title}</h1>
      <p className="sub">{g.summary}</p>
      {g.safety === 'caution' && !ended && path.length === 0 && (
        <p className="notice warn small">If the car feels unsafe to drive at any point, stop where it's safe and call roadside assistance.</p>
      )}

      {done.length > 0 && (
        <ol className="trail" aria-label="Steps so far">
          {done.map((p) => {
            const s = steps.find((x) => x.id === p.step)
            const label = s.type === 'ask' ? s.options[p.option].label : { fixed: 'Fixed it', not_fixed: "Didn't help", done: 'Done' }[p.answer]
            return <li key={p.step}><span>{s.text}</span><em>{label}</em></li>
          })}
        </ol>
      )}

      {step && (
        <section className={`stepcard${step.warn ? ' warn' : ''}`} aria-live="polite">
          <p className="step-text">{step.text}</p>
          {step.detail && <p className="step-detail">{step.detail}</p>}
          <div className="answers">
            {step.type === 'ask' && step.options.map((o, i) => (
              <button key={o.label} className="btn quiet block" onClick={() => answer('option', i)}>{o.label}</button>
            ))}
            {step.type === 'check' && step.confirm !== 'do' && (
              <>
                <button className="btn block" onClick={() => answer('fixed')}>That fixed it</button>
                <button className="btn quiet block" onClick={() => answer('not_fixed')}>Still not working</button>
              </>
            )}
            {step.type === 'check' && step.confirm === 'do' && <button className="btn block" onClick={() => answer('done')}>Done</button>}
            {step.type === 'info' && <button className="btn block" onClick={() => answer('seen')}>{step.next === 'done' ? 'Got it' : 'Continue'}</button>}
          </div>
        </section>
      )}

      {current === 'done' && (
        <section className="stepcard good">
          <p className="step-text">Glad that's sorted.</p>
          <p className="step-detail">If it comes back, run this guide again or ask for help from the Help tab.</p>
          <div className="answers"><Link className="btn block" to="/help">Back to Help</Link></div>
        </section>
      )}

      {current === 'escalate' && (
        <section className="stepcard">
          <p className="step-text">{g.escalate.text}</p>
          <p className="step-detail">Your service centre will want to know:</p>
          <ul className="tell">{g.escalate.tell_service.map((t) => <li key={t}>{t}</li>)}</ul>
          {g.reportable && <p className="step-detail">After saving, you can also report it to the manufacturer's engineers, if you agree.</p>}
          <div className="answers"><button className="btn block" onClick={() => setAsking(true)}>Save as a help request</button></div>
        </section>
      )}

      {path.length > 0 && <p style={{ marginTop: 12 }}><button className="linkbtn" onClick={back}>Back one step</button></p>}
      <ErrorText error={error} />
      {asking && (
        <RequestSheet cars={cars} car={car} guideId={g.id} transcript={path} onClose={() => setAsking(false)}
          onSaved={(c) => nav(`/help/requests/${c.id}`)} />
      )}
    </>
  )
}
