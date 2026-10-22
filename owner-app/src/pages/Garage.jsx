import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { CarRow } from './Home.jsx'

export default function Garage() {
  const [cars, setCars] = useState(null)
  const [error, setError] = useState('')
  useEffect(() => {
    api('/dashboard').then((d) => setCars(d.cars)).catch((e) => setError(e.message))
  }, [])
  if (error) return <p className="error">{error}</p>
  if (!cars) return <p className="muted">Loading…</p>
  return (
    <>
      {cars.length ? <div className="list">{cars.map((c) => <CarRow key={c.id} car={c} />)}</div> : (
        <div className="empty"><h3>Your garage is empty</h3><p>Add the cars you own or drive.</p></div>
      )}
      <div className="actions"><Link className="btn" to="/cars/new">Add a car</Link></div>
    </>
  )
}
