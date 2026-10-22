// The Indian number plate: each car's identity on every screen.
export default function Plate({ reg, size = 'm', label }) {
  return (
    <span className={`plate ${size}${reg ? '' : ' empty'}`} role="img" aria-label={label || `Registration ${reg || 'not entered'}`}>
      <span className="ind" aria-hidden="true">IND</span>
      <span className="reg" aria-hidden="true">{reg || 'MH 12 AB 1234'}</span>
    </span>
  )
}
