import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { FUELS, greeting, km, niceDate, when } from '../format.js'
import Plate from '../components/Plate.jsx'
import { BookingRow } from './Booking.jsx'

const SHORT = { rc: 'RC', insurance: 'insurance', puc: 'PUC' }

export function CarRow({ car }) {
  const d = car.documents || {}
  return (
    <Link className="car" to={`/cars/${car.id}`}>
      <Plate reg={car.reg_display} size="m" />
      <div className="line2">
        <span><strong style={{ color: 'var(--ink)' }}>{car.make} {car.model}</strong>, {FUELS[car.fuel]?.toLowerCase()}{car.role === 'driver' ? ' (shared with you)' : ''}</span>
        <span>{car.odometer_km != null ? km(car.odometer_km) : ''}</span>
      </div>
      {car.documents && (
        <div className="line2">
          <span className="dots" aria-label="Documents">
            {['expired', 'expiring', 'valid', 'no_expiry'].flatMap((k) => Array.from({ length: d[k] || 0 }, (_, i) => <i key={k + i} className={`dot ${k}`} />))}
            <span style={{ marginLeft: 6 }}>
              {d.expired ? `${d.expired} expired` : d.expiring ? `${d.expiring} expiring soon` : (d.valid || d.no_expiry) ? 'Documents in order' : 'No documents yet'}
            </span>
          </span>
          {car.missing_documents?.length > 0 && <span className="small">Add {car.missing_documents.map((m) => SHORT[m]).join(', ')}</span>}
        </div>
      )}
      {car.health && (
        <div className="line2 healthline">
          <span><i className={`hdot ${car.health.status}`} aria-hidden="true" />Health: {car.health.label.toLowerCase()}{car.health.top ? `, ${car.health.top}` : ''}</span>
        </div>
      )}
    </Link>
  )
}

export function AttentionRow({ item }) {
  const to = `/cars/${item.car_id}?tab=${item.type === 'document' ? 'documents' : 'reminders'}`
  return (
    <Link className={`row ${item.status}`} to={to}>
      <div className="grow">
        <div className="title">{item.title}</div>
        <div className="meta">{item.car}{item.due_km != null && item.type === 'reminder' ? `, at ${km(item.due_km)}` : ''}</div>
      </div>
      <span className={`when ${item.status}`}>{when(item)}</span>
    </Link>
  )
}

export default function Home() {
  const { user } = useAuth()
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  useEffect(() => { api('/dashboard').then(setData).catch((e) => setError(e.message)) }, [])
  if (error) return <p className="error">{error}</p>
  if (!data) return <p className="muted">Loading…</p>
  const n = data.counts.overdue + data.counts.due_soon
  return (
    <>
      <p className="greeting">{greeting(user.name)}</p>
      <p className="sub">
        {data.cars.length === 0 ? 'Add your car to keep its papers and reminders here.'
          : n === 0 ? 'Nothing needs your attention right now.'
            : `${n} ${n === 1 ? 'thing needs' : 'things need'} your attention.`}
      </p>
      {user.is_demo && <p className="notice small">This is a demo account. It and its data are deleted on {niceDate(user.demo_expires_at)}.</p>}
      {user.pending_invitations > 0 && (
        <p className="notice"><Link to="/me">Someone shared a car with you. Review the invitation.</Link></p>
      )}
      {data.attention.length > 0 && (
        <div className="list">{data.attention.map((i) => <AttentionRow key={i.type + i.id} item={i} />)}</div>
      )}
      {data.bookings?.length > 0 && (
        <>
          <h2>Service bookings</h2>
          <div className="list">{data.bookings.map((b) => <BookingRow key={b.id} b={b} showCar />)}</div>
        </>
      )}
      {data.cars.length > 0 && (
        <Link className="helpline" to="/help">
          <span className="grow"><strong>Something not working?</strong> Bluetooth, Android Auto, warning lights, ADAS</span>
          <span className="go" aria-hidden="true">›</span>
        </Link>
      )}
      <h2>Your cars</h2>
      {data.cars.length ? (
        <div className="list">{data.cars.map((c) => <CarRow key={c.id} car={c} />)}</div>
      ) : (
        <div className="empty">
          <h3>No car yet</h3>
          <p>Start with the registration number on your number plate. It takes a minute.</p>
          <Link className="btn" to="/cars/new">Add a car</Link>
        </div>
      )}
      {data.cars.length > 0 && <div className="actions"><Link className="btn quiet small" to="/cars/new">Add another car</Link></div>}
    </>
  )
}
