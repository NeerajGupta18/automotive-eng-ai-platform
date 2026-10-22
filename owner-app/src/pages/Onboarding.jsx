import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import Field, { ErrorText } from '../components/Field.jsx'

export default function Onboarding() {
  const { user, setUser, signOut } = useAuth()
  const [name, setName] = useState(user.name || '')
  const [consent, setConsent] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function save(e) {
    e.preventDefault()
    setBusy(true); setError('')
    try {
      setUser(await api('/me', { method: 'PATCH', body: { name, consent } }))
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  return (
    <div className="login">
      <span className="wordmark"><i aria-hidden="true" />CarSaathi</span>
      <h1 style={{ marginTop: 28 }}>Welcome. What should we call you?</h1>
      <p className="sub">Signed in as {user.email}.</p>
      <form onSubmit={save}>
        <Field label="Your name"><input autoComplete="name" required minLength={2} maxLength={60} value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <label className="check">
          <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} required />
          <span>
            CarSaathi may store the cars, documents and reminders I add, to show them to me and to the people I share a car with.
            I can download or delete everything at any time. <Link to="/privacy">Read the privacy notice</Link>.
          </span>
        </label>
        <ErrorText error={error} />
        <button className="btn block" disabled={busy || !consent || name.trim().length < 2}>Continue</button>
      </form>
      <p className="small" style={{ marginTop: 18 }}><button className="linkbtn" onClick={() => signOut()}>Sign out</button></p>
    </div>
  )
}
