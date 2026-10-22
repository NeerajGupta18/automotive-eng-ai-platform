import { useEffect, useRef, useState } from 'react'

// Two small charts for the Costs tab, drawn in SVG at the container's real width (so text stays its real size).
// Colours: fuel in the brand blue, other costs in orange (checked for colour-blind separation and contrast in light and
// dark); text always in ink colours; hover and keyboard focus show the values; every chart has a table view.

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
export const inr = (n, digits = 0) => `₹${Number(n || 0).toLocaleString('en-IN', { maximumFractionDigits: digits, minimumFractionDigits: digits })}`
const compact = (n) => (n >= 100000 ? `₹${+(n / 100000).toFixed(1)}L` : n >= 1000 ? `₹${+(n / 1000).toFixed(n >= 10000 ? 0 : 1)}K` : `₹${n}`)
const monthLabel = (k) => MONTHS[Number(k.slice(5)) - 1]

function useWidth() {
  const ref = useRef(null)
  const [w, setW] = useState(320)
  useEffect(() => {
    if (!ref.current) return undefined
    const ro = new ResizeObserver(([e]) => setW(Math.max(260, Math.round(e.contentRect.width))))
    ro.observe(ref.current)
    return () => ro.disconnect()
  }, [])
  return [ref, w]
}

function niceMax(v) {
  if (v <= 0) return 1000
  const p = 10 ** Math.floor(Math.log10(v))
  return [1, 2, 2.5, 5, 10].map((m) => m * p).find((m) => m >= v)
}

function Tip({ tip }) {
  if (!tip) return null
  return (
    <div className="viz-tip" style={{ left: tip.x, top: tip.y }} role="status">
      <div className="viz-tip-title">{tip.title}</div>
      {tip.rows.map((r) => (
        <div key={r.label} className="viz-tip-row">
          {r.key && <i className={`key ${r.key}`} aria-hidden="true" />}<strong>{r.value}</strong><span>{r.label}</span>
        </div>
      ))}
    </div>
  )
}

export function MonthlyBars({ months }) {
  const [ref, w] = useWidth()
  const [tip, setTip] = useState(null)
  const [table, setTable] = useState(false)
  const h = 180
  const pad = { l: 40, r: 6, t: 8, b: 22 }
  const max = niceMax(Math.max(...months.map((m) => m.fuel + m.other)))
  const band = (w - pad.l - pad.r) / months.length
  const bw = Math.min(24, band * 0.62)
  const y = (v) => pad.t + (h - pad.t - pad.b) * (1 - v / max)
  const ticks = [0, max / 2, max]
  const show = (m, i) => {
    const x = pad.l + band * i + band / 2
    setTip({ x: Math.min(Math.max(x, 70), w - 70), y: Math.max(0, y(m.fuel + m.other) - 8), title: `${monthLabel(m.month)} ${m.month.slice(0, 4)}`,
      rows: [{ key: 'fuel', value: inr(m.fuel), label: 'Fuel or charging' }, { key: 'other', value: inr(m.other), label: 'Other costs' },
        { value: inr(m.fuel + m.other), label: 'Total' }] })
  }
  return (
    <figure className="viz">
      <figcaption>
        <span className="viz-title">Spending by month</span>
        <span className="legend"><span><i className="key fuel rect" />Fuel or charging</span><span><i className="key other rect" />Other costs</span></span>
      </figcaption>
      <div className="viz-plot" ref={ref} onPointerLeave={() => setTip(null)}>
        <svg width={w} height={h} role="img" aria-label="Spending by month, fuel and other costs">
          {ticks.map((t) => (
            <g key={t}>
              <line x1={pad.l} x2={w - pad.r} y1={y(t)} y2={y(t)} className="grid" />
              <text x={pad.l - 6} y={y(t) + 4} className="tick" textAnchor="end">{compact(t)}</text>
            </g>
          ))}
          {months.map((m, i) => {
            const x = pad.l + band * i + (band - bw) / 2
            const fuelTop = y(m.fuel)
            const totalTop = y(m.fuel + m.other)
            const gap = m.fuel > 0 && m.other > 0 ? 2 : 0
            return (
              <g key={m.month}>
                {m.fuel > 0 && <path className="bar fuel" d={col(x, fuelTop, bw, y(0), m.other > 0 ? 0 : 4)} />}
                {m.other > 0 && <path className="bar other" d={col(x, totalTop, bw, fuelTop - gap, 4)} />}
                {(i % (months.length > 6 ? 2 : 1) === (months.length - 1) % (months.length > 6 ? 2 : 1)) && (
                  <text x={x + bw / 2} y={h - 6} className="tick" textAnchor="middle">{monthLabel(m.month)}</text>
                )}
                <rect className="hit" x={pad.l + band * i} y={pad.t} width={band} height={h - pad.t - pad.b} tabIndex={0}
                  aria-label={`${monthLabel(m.month)}: fuel ${inr(m.fuel)}, other ${inr(m.other)}`}
                  onPointerMove={() => show(m, i)} onFocus={() => show(m, i)} onBlur={() => setTip(null)} />
              </g>
            )
          })}
        </svg>
        <Tip tip={tip} />
      </div>
      <button className="linkbtn small" onClick={() => setTable(!table)}>{table ? 'Hide table' : 'Show as table'}</button>
      {table && (
        <table className="viz-table">
          <thead><tr><th>Month</th><th>Fuel or charging</th><th>Other</th><th>Total</th></tr></thead>
          <tbody>{months.map((m) => <tr key={m.month}><td>{monthLabel(m.month)} {m.month.slice(0, 4)}</td><td>{inr(m.fuel)}</td><td>{inr(m.other)}</td><td>{inr(m.fuel + m.other)}</td></tr>)}</tbody>
        </table>
      )}
    </figure>
  )
}

// a column with a 4px rounded top and a square foot on the baseline
function col(x, top, w, bottom, r) {
  const hgt = Math.max(0, bottom - top)
  const rr = Math.min(r, hgt, w / 2)
  return `M${x},${bottom}V${top + rr}Q${x},${top} ${x + rr},${top}H${x + w - rr}Q${x + w},${top} ${x + w},${top + rr}V${bottom}Z`
}

export function MileageLine({ series, unit, usual }) {
  const [ref, w] = useWidth()
  const [hover, setHover] = useState(null)
  const [table, setTable] = useState(false)
  const h = 170
  const pad = { l: 34, r: 14, t: 12, b: 22 }
  const vals = series.map((p) => p.mileage).concat(usual ? [usual] : [])
  let lo = Math.floor(Math.min(...vals) - 1)
  let hi = Math.ceil(Math.max(...vals) + 1)
  if (hi - lo < 4) { lo -= 2; hi += 2 }
  lo = Math.max(0, lo)
  const x = (i) => pad.l + (series.length === 1 ? (w - pad.l - pad.r) / 2 : ((w - pad.l - pad.r) * i) / (series.length - 1))
  const y = (v) => pad.t + (h - pad.t - pad.b) * (1 - (v - lo) / (hi - lo))
  const d = series.map((p, i) => `${i ? 'L' : 'M'}${x(i)},${y(p.mileage)}`).join('')
  const last = series.length - 1
  const pick = (e) => {
    const r = e.currentTarget.getBoundingClientRect()
    const px = e.clientX - r.left
    let best = 0
    series.forEach((_, i) => { if (Math.abs(x(i) - px) < Math.abs(x(best) - px)) best = i })
    setHover(best)
  }
  const p = hover != null ? series[hover] : null
  const date = (iso) => `${Number(iso.slice(8, 10))} ${MONTHS[Number(iso.slice(5, 7)) - 1]}`
  return (
    <figure className="viz">
      <figcaption>
        <span className="viz-title">Mileage, km per {unit}, fill to fill</span>
        {usual && <span className="legend"><span><i className="key ref" />usual {usual}</span></span>}
      </figcaption>
      <div className="viz-plot" ref={ref}>
        <svg width={w} height={h} role="img" aria-label={`Mileage over the last ${series.length} fills`}
          onPointerMove={pick} onPointerLeave={() => setHover(null)}>
          {[lo, Math.round((lo + hi) / 2), hi].map((t) => (
            <g key={t}><line x1={pad.l} x2={w - pad.r} y1={y(t)} y2={y(t)} className="grid" /><text x={pad.l - 6} y={y(t) + 4} className="tick" textAnchor="end">{t}</text></g>
          ))}
          {usual && <line x1={pad.l} x2={w - pad.r} y1={y(usual)} y2={y(usual)} className="ref" />}
          <path d={d} className="line fuel" />
          {hover != null && <line x1={x(hover)} x2={x(hover)} y1={pad.t} y2={h - pad.b} className="cross" />}
          {series.map((s, i) => (i === last || i === hover) && <circle key={i} cx={x(i)} cy={y(s.mileage)} r="4.5" className="dot fuel" />)}
          <text x={x(last) - 8} y={y(series[last].mileage) + (last && series[last].mileage < series[last - 1].mileage ? 16 : -10)}
            className="tick strong" textAnchor="end">{series[last].mileage}</text>
          <text x={pad.l} y={h - 6} className="tick">{date(series[0].on)}</text>
          <text x={w - pad.r} y={h - 6} className="tick" textAnchor="end">{date(series[last].on)}</text>
          <rect className="hit" x={pad.l} y={pad.t} width={w - pad.l - pad.r} height={h - pad.t - pad.b} tabIndex={0}
            aria-label="Mileage chart: use the arrow keys to read each fill"
            onKeyDown={(e) => {
              if (e.key === 'ArrowRight') setHover((v) => Math.min(last, (v ?? -1) + 1))
              if (e.key === 'ArrowLeft') setHover((v) => Math.max(0, (v ?? last + 1) - 1))
            }} onBlur={() => setHover(null)} />
        </svg>
        {p && (
          <Tip tip={{ x: Math.min(Math.max(x(hover), 70), w - 70), y: Math.max(0, y(p.mileage) - 12), title: `Fill on ${date(p.on)}`,
            rows: [{ key: 'fuel', value: `${p.mileage} km/${unit}`, label: `${p.distance_km.toLocaleString('en-IN')} km on ${p.quantity} ${unit}` },
              { value: `₹${p.cost_per_km}`, label: 'per km' }] }} />
        )}
      </div>
      <button className="linkbtn small" onClick={() => setTable(!table)}>{table ? 'Hide table' : 'Show as table'}</button>
      {table && (
        <table className="viz-table">
          <thead><tr><th>Full fill on</th><th>km driven</th><th>{unit} used</th><th>km/{unit}</th><th>₹ per km</th></tr></thead>
          <tbody>{series.map((s) => <tr key={s.on + s.to_km}><td>{date(s.on)}</td><td>{s.distance_km.toLocaleString('en-IN')}</td><td>{s.quantity}</td><td>{s.mileage}</td><td>{s.cost_per_km}</td></tr>)}</tbody>
        </table>
      )}
    </figure>
  )
}
