import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, download } from '../api.js'
import { useAuth } from '../auth.jsx'
import { niceDate } from '../format.js'
import Field, { ErrorText } from '../components/Field.jsx'
import InstallApp from '../components/InstallApp.jsx'
import Sheet from '../components/Sheet.jsx'

export default function Profile() {
  const { user, setUser, signOut, config } = useAuth()
  const [name, setName] = useState(user.name)
  const [phone, setPhone] = useState(user.phone || '')
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')
  const [invites, setInvites] = useState([])
  const [deleting, setDeleting] = useState(false)

  const loadInvites = () => api('/invitations').then(setInvites).catch(() => {})
  useEffect(() => { loadInvites() }, [])

  async function save(e) {
    e.preventDefault()
    setError(''); setMsg('')
    try { setUser(await api('/me', { method: 'PATCH', body: { name, phone } })); setMsg('Saved.') } catch (err) { setError(err.message) }
  }
  async function respond(i, accept) {
    try {
      await api(`/invitations/${i.id}/${accept ? 'accept' : 'decline'}`, { method: 'POST' })
      loadInvites(); setUser(await api('/me'))
    } catch (err) { setError(err.message) }
  }

  return (
    <>
      {user.is_demo && <p className="notice">Demo account. It is deleted on {niceDate(user.demo_expires_at)}; sign in with your email to keep your data.</p>}
      {invites.length > 0 && (
        <>
          <h2 style={{ marginTop: 6 }}>Invitations</h2>
          <div className="list">
            {invites.map((i) => (
              <div key={i.id} className="row">
                <div className="grow"><div className="title">{i.car} ({i.reg_display})</div><div className="meta">{i.from} invited you as {i.role}</div></div>
                <button className="btn small" onClick={() => respond(i, true)}>Accept</button>
                <button className="linkbtn small" onClick={() => respond(i, false)}>Decline</button>
              </div>
            ))}
          </div>
        </>
      )}
      <h2 style={{ marginTop: invites.length ? 28 : 6 }}>Your details</h2>
      <form onSubmit={save}>
        <Field label="Name"><input required minLength={2} maxLength={60} value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Email" hint="You sign in with this address"><input value={user.email} disabled /></Field>
        <Field label="Mobile number" hint="Optional. Filled in when you book a service, as the number the workshop calls.">
          <input inputMode="tel" autoComplete="tel" value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="98765 43210" />
        </Field>
        <ErrorText error={error} />
        {msg && <p className="small" role="status">{msg}</p>}
        <button className="btn">Save details</button>
      </form>

      <h2>Notifications</h2>
      <div className="list">
        <Link className="row" to="/me/notifications">
          <div className="grow"><div className="title">Reminders and updates</div><div className="meta">What you're told about, by email and on your devices</div></div>
          <span className="go" aria-hidden="true">›</span>
        </Link>
      </div>

      <h2>Get the app</h2>
      <InstallApp android={config?.android_app} />
      <p className="small muted">The app shows the same account on every device you sign in on.</p>

      <h2>Your data</h2>
      <p className="small">Download everything we store about you, or delete your account. <Link to="/privacy">Privacy notice</Link></p>
      <div className="actions">
        <button className="btn quiet" onClick={() => download('/me/export', 'my-car-data.json').catch((e) => setError(e.message))}>Download my data</button>
        <button className="btn quiet" onClick={() => setDeleting(true)}>Delete my account</button>
      </div>

      <h2>Sign out</h2>
      <div className="actions">
        <button className="btn ghost" onClick={() => signOut()}>Sign out</button>
        <button className="btn quiet" onClick={() => signOut(true)}>Sign out on all devices</button>
      </div>
      {deleting && <DeleteSheet onClose={() => setDeleting(false)} />}
    </>
  )
}

function DeleteSheet({ onClose }) {
  const { signOut } = useAuth()
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState('')
  async function del(e) {
    e.preventDefault()
    try {
      await api('/me/delete', { method: 'POST', body: { confirm } })
      await signOut()
    } catch (err) { setError(err.message) }
  }
  return (
    <Sheet title="Delete your account?" onClose={onClose}>
      <form onSubmit={del}>
        <p>Your profile, sign-ins and every car you are the only owner of are deleted, with their documents and reminders. Cars that another
          owner shares with you stay with them. This cannot be undone.</p>
        <Field label="Type DELETE to confirm"><input value={confirm} onChange={(e) => setConfirm(e.target.value)} autoCapitalize="characters" /></Field>
        <ErrorText error={error} />
        <div className="actions"><button className="btn danger" disabled={confirm !== 'DELETE'}>Delete my account</button></div>
      </form>
    </Sheet>
  )
}
