export default function Field({ label, hint, children }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint && <small className="hint">{hint}</small>}
    </label>
  )
}

export function Segment({ options, value, onChange, label }) {
  return (
    <div className="segment" role="group" aria-label={label}>
      {Object.entries(options).map(([k, v]) => (
        <button type="button" key={k} aria-pressed={value === k} onClick={() => onChange(k)}>{v}</button>
      ))}
    </div>
  )
}

export function ErrorText({ error }) {
  return error ? <p className="error" role="alert">{error}</p> : null
}
