import { useEffect, useRef, useState } from 'react'
import { Navigate, NavLink, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { api } from './api.js'
import { useAuth } from './auth.jsx'
import { BellIcon, CarIcon, HelpIcon, HomeIcon, InboxIcon, UserIcon } from './components/Icons.jsx'
import { pushSupport, syncNative } from './lib/notify.js'
import BookService from './pages/BookService.jsx'
import Booking from './pages/Booking.jsx'
import CarDetail from './pages/CarDetail.jsx'
import CarForm from './pages/CarForm.jsx'
import Garage from './pages/Garage.jsx'
import Guide from './pages/Guide.jsx'
import Help, { AllRequests } from './pages/Help.jsx'
import HelpRequest from './pages/HelpRequest.jsx'
import Home from './pages/Home.jsx'
import Lights from './pages/Lights.jsx'
import Login from './pages/Login.jsx'
import NotificationSettings from './pages/NotificationSettings.jsx'
import Notifications from './pages/Notifications.jsx'
import Onboarding from './pages/Onboarding.jsx'
import Privacy from './pages/Privacy.jsx'
import Profile from './pages/Profile.jsx'
import Reminders from './pages/Reminders.jsx'
import Workshop from './pages/Workshop.jsx'

const TITLES = [[/^\/garage/, 'Garage'], [/^\/cars\/new/, 'Add a car'], [/^\/cars\/[^/]+\/edit/, 'Edit car'],
  [/^\/cars\/[^/]+\/book/, 'Book a service'], [/^\/cars\//, 'Car'], [/^\/bookings\//, 'Service booking'],
  [/^\/notifications/, 'Notifications'], [/^\/me\/notifications/, 'Notification settings'],
  [/^\/reminders/, 'Reminders'], [/^\/me/, 'Profile'], [/^\/help\/lights/, 'Warning lights'], [/^\/help\/requests\/./, 'Help request'],
  [/^\/help\/requests/, 'Help requests'], [/^\/help\/guide/, 'Help'], [/^\/help/, 'Help']]

function Offline() {
  const [off, setOff] = useState(!navigator.onLine)
  useEffect(() => {
    const on = () => setOff(false)
    const offf = () => setOff(true)
    window.addEventListener('online', on); window.addEventListener('cs-online', on)
    window.addEventListener('offline', offf); window.addEventListener('cs-offline', offf)
    return () => {
      window.removeEventListener('online', on); window.removeEventListener('cs-online', on)
      window.removeEventListener('offline', offf); window.removeEventListener('cs-offline', offf)
    }
  }, [])
  return off ? <div className="offline" role="status">You are offline. Changes need a connection.</div> : null
}

// The unread count of the inbox: on start, when the page changes (at most every 30 s), when the app comes back to the front,
// and straight away when the inbox changes it.
function useUnread(pathname) {
  const [n, setN] = useState(0)
  const last = useRef(0)
  useEffect(() => {
    const got = (e) => setN(e.detail)
    const fetchNow = () => { last.current = Date.now(); api('/notifications/unread').then((r) => setN(r.unread)).catch(() => {}) }
    const back = () => { if (document.visibilityState === 'visible') fetchNow() }
    window.addEventListener('cs-unread', got)
    document.addEventListener('visibilitychange', back)
    return () => { window.removeEventListener('cs-unread', got); document.removeEventListener('visibilitychange', back) }
  }, [])
  useEffect(() => {
    if (Date.now() - last.current < 30_000) return
    last.current = Date.now()
    api('/notifications/unread').then((r) => setN(r.unread)).catch(() => {})
  }, [pathname])
  return n
}

function Shell() {
  const loc = useLocation()
  const nav = useNavigate()
  const { user, config } = useAuth()
  const title = TITLES.find(([re]) => re.test(loc.pathname))?.[1]
  const unread = useUnread(loc.pathname)
  useEffect(() => { window.scrollTo(0, 0) }, [loc.pathname])
  // the Android app schedules the coming weeks' reminders on the phone (it can't receive Web Push)
  useEffect(() => { if (pushSupport() === 'native') syncNative(true, nav).catch(() => {}) }, [nav])
  return (
    <div className="app">
      <header className="topbar">
        {title ? (
          <>
            {/^\/(cars|help\/|bookings|notifications|me\/)/.test(loc.pathname) && <button className="back" onClick={() => nav(-1)} aria-label="Back">‹</button>}
            <h1>{title}</h1>
          </>
        ) : (
          <span className="wordmark" style={{ flex: 1 }}><i aria-hidden="true" />{config?.app_name || 'CarSaathi'}</span>
        )}
        {!/^\/notifications/.test(loc.pathname) && (
          <NavLink to="/notifications" className="inboxbtn" aria-label={unread ? `Notifications, ${unread} unread` : 'Notifications'}>
            <InboxIcon />{unread > 0 && <span className="count" aria-hidden="true">{unread > 99 ? '99+' : unread}</span>}
          </NavLink>
        )}
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/garage" element={<Garage />} />
          <Route path="/cars/new" element={<CarForm />} />
          <Route path="/cars/:id/edit" element={<CarForm />} />
          <Route path="/cars/:id" element={<CarDetail />} />
          <Route path="/cars/:id/book" element={<BookService />} />
          <Route path="/bookings/:id" element={<Booking />} />
          <Route path="/notifications" element={<Notifications />} />
          <Route path="/me/notifications" element={<NotificationSettings />} />
          <Route path="/reminders" element={<Reminders />} />
          <Route path="/help" element={<Help />} />
          <Route path="/help/guide/:id" element={<Guide />} />
          <Route path="/help/lights" element={<Lights />} />
          <Route path="/help/requests" element={<AllRequests />} />
          <Route path="/help/requests/:id" element={<HelpRequest />} />
          <Route path="/me" element={<Profile />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
      <div className="tabs">
        <nav aria-label="Main">
          <NavLink to="/" end><HomeIcon />Home</NavLink>
          <NavLink to="/garage"><CarIcon />Garage</NavLink>
          <NavLink to="/help"><HelpIcon />Help</NavLink>
          <NavLink to="/reminders"><BellIcon />Reminders</NavLink>
          <NavLink to="/me" aria-label={user.pending_invitations ? 'Profile, invitation waiting' : 'Profile'}>
            <span style={{ position: 'relative' }}><UserIcon />{user.pending_invitations > 0 && <i className="dot expired" style={{ position: 'absolute', top: 0, right: -4 }} />}</span>
            Profile
          </NavLink>
        </nav>
      </div>
    </div>
  )
}

export default function App() {
  const { user, loading } = useAuth()
  const loc = useLocation()
  if (loc.pathname === '/privacy') return <Privacy />
  if (loc.pathname === '/w') return <Workshop />                       // a workshop answering a booking: no account
  if (loading) return <div className="splash"><span className="wordmark"><i aria-hidden="true" />CarSaathi</span></div>
  return (
    <>
      <Offline />
      {!user ? <Login /> : !user.profile_complete ? <Onboarding /> : <Shell />}
    </>
  )
}
