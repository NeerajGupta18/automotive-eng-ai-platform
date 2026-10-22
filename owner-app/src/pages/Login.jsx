import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import Field, { ErrorText } from '../components/Field.jsx'
import Plate from '../components/Plate.jsx'
import InstallApp from '../components/InstallApp.jsx'

export default function Login() {
  const { config, signIn } = useAuth()
  const [email, setEmail] = useState('')
  const [code, setCode] = useState('')
  const [step, setStep] = useState('email')
  const [sent, setSent] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [wait, setWait] = useState(0)
  const name = config?.app_name || 'CarSaathi'

  useEffect(() => {
    if (!wait) return
    const t = setTimeout(() => setWait(wait - 1), 1000)
    return () => clearTimeout(t)
  }, [wait])

  async function sendCode(e) {
    e && e.preventDefault()
    setBusy(true); setError('')
    try {
      const r = await api('/auth/code', { method: 'POST', body: { email } })
      setSent(r); setStep('code'); setWait(30); setCode('')
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  async function verify(e) {
    e.preventDefault()
    setBusy(true); setError('')
    try {
      const r = await api('/auth/verify', { method: 'POST', body: { email, code } })
      signIn(r.token, r.user)
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  async function demo() {
    setBusy(true); setError('')
    try {
      const r = await api('/auth/demo', { method: 'POST' })
      signIn(r.token, r.user)
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  return (
    <div className="login">
      <span className="wordmark"><i aria-hidden="true" />{name}</span>
      <div className="big-plate"><Plate reg="MH 12 AB 1234" size="l" label="Example number plate" /></div>
      <h1>Your car's papers and upkeep, sorted.</h1>
      <p className="sub">Keep the RC, insurance and PUC on your phone, get reminded before they expire, and share the car with your family.</p>

      {config && !config.email_signin && (
        <p className="notice warn">Email sign-in is not set up on this server yet. You can try the demo.</p>
      )}

      {config?.email_signin !== false && step === 'email' && (
        <form onSubmit={sendCode}>
          <Field label="Email">
            <input type="email" inputMode="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)}
                   placeholder="you@example.com" />
          </Field>
          <ErrorText error={error} />
          <button className="btn block" disabled={busy || !email}>{busy ? 'Sending…' : 'Send me a sign-in code'}</button>
        </form>
      )}

      {step === 'code' && (
        <form onSubmit={verify}>
          <p>We sent a 6-digit code to <strong>{email}</strong>. It is valid for {sent?.expires_in_min || 10} minutes.</p>
          {sent?.delivery === 'console' && (
            <p className="notice small">Development server: the code is printed in the server log instead of being emailed.</p>
          )}
          <Field label="Sign-in code">
            <input className="code-input" inputMode="numeric" autoComplete="one-time-code" maxLength={6} required
                   value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))} />
          </Field>
          <ErrorText error={error} />
          <button className="btn block" disabled={busy || code.length !== 6}>{busy ? 'Checking…' : 'Sign in'}</button>
          <div className="actions" style={{ justifyContent: 'space-between' }}>
            <button type="button" className="linkbtn" onClick={() => { setStep('email'); setError('') }}>Use another email</button>
            <button type="button" className="linkbtn" disabled={wait > 0 || busy} onClick={sendCode}>
              {wait > 0 ? `Send again in ${wait}s` : 'Send a new code'}
            </button>
          </div>
        </form>
      )}

      {config?.demo && step === 'email' && (
        <>
          <div className="or">or</div>
          <button className="btn ghost block" onClick={demo} disabled={busy}>Try the demo</button>
          <p className="small muted" style={{ marginTop: 8 }}>
            A private sample garage with two cars. It is deleted after {config.demo_hours} hours.
          </p>
          {step === 'email' && error && <ErrorText error={error} />}
        </>
      )}

      <InstallApp android={config?.android_app} />
      <p className="small muted" style={{ marginTop: 24 }}><Link to="/privacy">How we handle your data</Link></p>
    </div>
  )
}
