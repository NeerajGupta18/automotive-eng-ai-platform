import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import Field, { ErrorText } from '../components/Field.jsx'
import Sheet from '../components/Sheet.jsx'
import { niceDate } from '../format.js'

const REPORT_STATUS = {
  not_connected: ['Saved, not sent', "This app isn't connected to the manufacturer's engineering system yet, so nothing was sent. Your consent and the report are kept with this request."],
  dry_run: ['Prepared, not sent', 'This server runs in demo mode: the report was prepared and checked, but not sent to the manufacturer.'],
  pending: ['Waiting to send', "The manufacturer's system didn't answer yet. CarSaathi keeps trying for a while."],
  sent: ['Sent to the manufacturer', 'The engineers received it with the reference below. They may group it with similar reports.'],
  failed: ["Couldn't be sent", "The manufacturer's system didn't accept it after several tries."],
}

function Photo({ caseId, p, onDelete }) {
  const [url, setUrl] = useState('')
  useEffect(() => {
    let u = ''
    api(`/help/requests/${caseId}/photos/${p.id}`, { raw: true }).then((r) => r.blob()).then((b) => { u = URL.createObjectURL(b); setUrl(u) }).catch(() => {})
    return () => u && URL.revokeObjectURL(u)
  }, [caseId, p.id])
  return (
    <figure className="thumb">
      {url ? <img src={url} alt={`Photo ${p.file_name}`} /> : <div className="ph" />}
      <button className="linkbtn small" onClick={onDelete}>Remove</button>
    </figure>
  )
}

export default function HelpRequest() {
  const { id } = useParams()
  const nav = useNavigate()
  const [cs, setCs] = useState(null)
  const [sheet, setSheet] = useState(null)
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')
  const load = useCallback(() => api(`/help/requests/${id}`).then(setCs).catch((e) => setError(e.message)), [id])
  useEffect(() => { load() }, [load])
  if (error && !cs) return <p className="error">{error}</p>
  if (!cs) return <p className="muted">Loading…</p>

  async function share() {
    setMsg('')
    try {
      const text = await (await api(`/help/requests/${id}/summary`, { raw: true })).text()
      if (navigator.share) {
        await navigator.share({ title: `Help request ${cs.ref}`, text })
      } else {
        await navigator.clipboard.writeText(text)
        setMsg('Copied. Paste it into WhatsApp or an email to your service centre.')
      }
    } catch (err) {
      if (err?.name !== 'AbortError') setSheet({ type: 'text' })
    }
  }
  async function setStatus(status) {
    try { setCs(await api(`/help/requests/${id}`, { method: 'PATCH', body: { status } })) } catch (err) { setError(err.message) }
  }
  async function addPhoto(e) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    const form = new FormData(); form.append('file', file)
    try { setCs(await api(`/help/requests/${id}/photos`, { method: 'POST', form })); setError('') } catch (err) { setError(err.message) }
  }
  async function removePhoto(pid) {
    try { await api(`/help/requests/${id}/photos/${pid}`, { method: 'DELETE' }); load() } catch (err) { setError(err.message) }
  }
  async function remove() {
    if (!window.confirm('Delete this help request?')) return
    try { await api(`/help/requests/${id}`, { method: 'DELETE' }); nav('/help', { replace: true }) } catch (err) { setError(err.message) }
  }

  const answers = cs.transcript.filter((t) => t.type === 'ask')
  const tried = cs.transcript.filter((t) => t.type === 'check')
  const rep = cs.report && REPORT_STATUS[cs.report.status]
  return (
    <>
      <h1 className="guide-title">{cs.title}</h1>
      <p className="sub">{[cs.ref, cs.car_label, cs.happened_on && `noticed ${niceDate(cs.happened_on)}`].filter(Boolean).join(', ')}</p>
      {cs.status === 'resolved' && <p className="notice good small">Marked as resolved on {niceDate(cs.resolved_at)}.</p>}

      <div className="actions" style={{ marginTop: 0 }}>
        <button className="btn" onClick={share}>Send to your service centre</button>
        {cs.status === 'open' && cs.car_id && <Link className="btn quiet" to={`/cars/${cs.car_id}/book?help=${cs.id}`}>Book a visit</Link>}
        <button className="btn quiet" onClick={() => setSheet({ type: 'edit' })}>Edit</button>
      </div>
      {msg && <p className="notice good small" role="status">{msg}</p>}
      <ErrorText error={error} />

      {cs.description && (<><h2>What happened</h2><p style={{ whiteSpace: 'pre-wrap' }}>{cs.description}</p></>)}
      {answers.length > 0 && (
        <>
          <h2>Your answers</h2>
          <ul className="tried">{answers.map((t) => <li key={t.step}><span>{t.text}</span><em>{t.answer_text}</em></li>)}</ul>
        </>
      )}
      {tried.length > 0 && (
        <>
          <h2>Already tried</h2>
          <ul className="tried">{tried.map((t) => <li key={t.step}><span>{t.text}</span><em>{t.answer_text}</em></li>)}</ul>
        </>
      )}

      <h2>Photos</h2>
      <p className="small muted">A photo of the message on the screen or the warning light helps the service centre. Up to 3.</p>
      <div className="thumbs">
        {cs.photos.map((p) => <Photo key={p.id} caseId={id} p={p} onDelete={() => removePhoto(p.id)} />)}
        {cs.photos.length < 3 && (
          <label className="thumb add">
            <input type="file" accept="image/*" capture="environment" onChange={addPhoto} />
            <span>Add photo</span>
          </label>
        )}
      </div>

      {cs.reportable && (
        <>
          <h2>Report to the manufacturer</h2>
          {!cs.report ? (
            <div className="report">
              <p>Reports like this help the engineers find out why driver-assistance systems react to things that aren't there, or don't
                react when they should. Nothing is shared unless you agree, and never your name, phone, email or number plate.</p>
              <button className="btn ghost" onClick={() => setSheet({ type: 'report' })}>Review and report</button>
            </div>
          ) : (
            <div className={`report ${cs.report.status}`}>
              <h3>{rep[0]}</h3>
              <p className="small">{rep[1]}</p>
              {cs.report.remote_ref && <p className="small">Reference: <strong>{cs.report.remote_ref}</strong></p>}
              <details><summary className="small">What was shared</summary><p className="small mono">{cs.report.payload.text}</p>
                {cs.report.payload.vin && <p className="small">VIN: {cs.report.payload.vin}</p>}</details>
            </div>
          )}
        </>
      )}

      {cs.tell_service.length > 0 && (
        <>
          <h2>Your service centre will want to know</h2>
          <ul className="tell">{cs.tell_service.map((t) => <li key={t}>{t}</li>)}</ul>
        </>
      )}

      <div className="actions">
        {cs.status === 'open'
          ? <button className="btn quiet" onClick={() => setStatus('resolved')}>Mark as resolved</button>
          : <button className="btn quiet" onClick={() => setStatus('open')}>Reopen</button>}
        <button className="btn quiet" onClick={remove}>Delete</button>
      </div>

      {sheet?.type === 'edit' && <EditSheet cs={cs} onClose={() => setSheet(null)} onSaved={(c) => { setCs(c); setSheet(null) }} />}
      {sheet?.type === 'report' && <ReportSheet cs={cs} onClose={() => setSheet(null)} onSaved={(c) => { setCs(c); setSheet(null) }} />}
      {sheet?.type === 'text' && <TextSheet id={id} onClose={() => setSheet(null)} />}
    </>
  )
}

function EditSheet({ cs, onClose, onSaved }) {
  const [f, setF] = useState({ title: cs.title, description: cs.description })
  const [error, setError] = useState('')
  async function save(e) {
    e.preventDefault()
    try { onSaved(await api(`/help/requests/${cs.id}`, { method: 'PATCH', body: f })) } catch (err) { setError(err.message) }
  }
  return (
    <Sheet title="Edit help request" onClose={onClose}>
      <form onSubmit={save}>
        <Field label="The problem in a few words"><input required minLength={3} maxLength={120} value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} /></Field>
        <Field label="What happened"><textarea maxLength={2000} value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
        <ErrorText error={error} />
        <div className="actions"><button className="btn">Save changes</button></div>
      </form>
    </Sheet>
  )
}

function TextSheet({ id, onClose }) {
  const [text, setText] = useState('')
  useEffect(() => { api(`/help/requests/${id}/summary`, { raw: true }).then((r) => r.text()).then(setText).catch(() => {}) }, [id])
  return (
    <Sheet title="Summary for your service centre" onClose={onClose}>
      <p className="small muted">Select the text and copy it into WhatsApp or an email.</p>
      <textarea className="summary" readOnly value={text} onFocus={(e) => e.target.select()} />
    </Sheet>
  )
}

function ReportSheet({ cs, onClose, onSaved }) {
  const [f, setF] = useState({ speed_kmh: '', sw_version: '', include_vin: false, consent: false })
  const [preview, setPreview] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const body = { speed_kmh: f.speed_kmh === '' ? null : f.speed_kmh, sw_version: f.sw_version || null, include_vin: f.include_vin }
  const key = JSON.stringify(body)
  useEffect(() => {
    const t = setTimeout(() => {
      api(`/help/requests/${cs.id}/report/preview`, { method: 'POST', body: JSON.parse(key) })
        .then((p) => { setPreview(p); setError('') }).catch((e) => setError(e.message))
    }, 250)
    return () => clearTimeout(t)
  }, [cs.id, key])
  async function send(e) {
    e.preventDefault()
    setBusy(true); setError('')
    try { onSaved(await api(`/help/requests/${cs.id}/report`, { method: 'POST', body: { ...body, consent: f.consent } })) }
    catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  return (
    <Sheet title="Report to the manufacturer" onClose={onClose}>
      <form onSubmit={send}>
        <div className="two">
          <Field label="Speed (km/h)" hint="Roughly, if you remember">
            <input inputMode="numeric" maxLength={3} value={f.speed_kmh} onChange={(e) => setF({ ...f, speed_kmh: e.target.value.replace(/\D/g, '') })} />
          </Field>
          <Field label="Software version" hint="Optional, e.g. 2.4.0 (Settings > About)">
            <input value={f.sw_version} maxLength={20} onChange={(e) => setF({ ...f, sw_version: e.target.value.trim() })} />
          </Field>
        </div>
        {cs.car_has_vin ? (
          <label className="check">
            <input type="checkbox" checked={f.include_vin} onChange={(e) => setF({ ...f, include_vin: e.target.checked })} />
            <span>Include my car's VIN, so the engineers can find its exact software and build. <span className="muted">Optional.</span></span>
          </label>
        ) : <p className="small muted">Add the VIN to the car (Details, Edit) if you want the engineers to see the exact build. Optional.</p>}
        <p className="field"><span>Exactly what will be shared</span></p>
        <div className="shared">
          {preview ? (
            <>
              <p className="mono small">{preview.item.text}</p>
              <p className="small muted">Date: {niceDate(preview.item.reported_at)}{preview.item.vin ? `. VIN: ${preview.item.vin}` : ''}{preview.item.build ? `. Software ${preview.item.build}` : ''}.
                Not shared: your name, email, phone, number plate, your own description and photos.</p>
            </>
          ) : <p className="small muted">Preparing…</p>}
        </div>
        <label className="check">
          <input type="checkbox" required checked={f.consent} onChange={(e) => setF({ ...f, consent: e.target.checked })} />
          <span>I agree to share this report with the car manufacturer's engineering team to investigate the problem.</span>
        </label>
        <ErrorText error={error} />
        <div className="actions"><button className="btn" disabled={busy || !f.consent || !preview}>{busy ? 'Sending…' : 'Share the report'}</button></div>
      </form>
    </Sheet>
  )
}
