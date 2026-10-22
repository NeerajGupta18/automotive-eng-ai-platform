const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

export function niceDate(iso) {
  if (!iso) return ''
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number)
  return `${d} ${months[m - 1]} ${y}`
}

export function km(n) {
  return n == null ? '' : `${Number(n).toLocaleString('en-IN')} km`
}

// When something is due, by date, by odometer reading, or both (whichever comes first matters)
export function when(item) {
  const d = item.days_left
  const k = item.km_left
  if (item.status === 'done') return 'Done'
  if (k != null && k < 0 && !(d != null && d < 0)) return `${km(-k)} overdue`
  let byDate = ''
  if (d != null) {
    if (d < -1) return `${-d} days overdue`
    if (d === -1) return 'Since yesterday'
    byDate = d === 0 ? 'Today' : d === 1 ? 'Tomorrow' : d <= 60 ? `In ${d} days` : niceDate(item.due_on || item.expires_on)
  }
  if (k != null) {
    if (byDate) return `${byDate} or ${km(k)}`
    if (item.predicted_on) {
      const [, m, day] = item.predicted_on.split('-').map(Number)
      return `In ${km(k)}, about ${day} ${months[m - 1]}` // at the car's usual km per day
    }
    return `In ${km(k)}`
  }
  return byDate
}

export function greeting(name) {
  const h = new Date().getHours()
  const part = h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening'
  return `${part}, ${(name || '').split(' ')[0]}`
}

export const FUELS = { petrol: 'Petrol', diesel: 'Diesel', cng: 'CNG', electric: 'Electric', hybrid: 'Hybrid' }
