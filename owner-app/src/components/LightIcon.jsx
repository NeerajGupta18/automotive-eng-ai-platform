// Dashboard warning light symbols, simplified from the ISO 2575 shapes most cars use. Drawn in currentColor.
const S = { fill: 'none', stroke: 'currentColor', strokeWidth: 3, strokeLinecap: 'round', strokeLinejoin: 'round' }
const T = { fill: 'currentColor', stroke: 'none', fontFamily: 'Barlow Semi Condensed, Barlow, sans-serif', fontWeight: 700, textAnchor: 'middle' }

const Bang = ({ x = 24, y = 17, h = 10 }) => (<><path d={`M${x} ${y}v${h}`} /><circle cx={x} cy={y + h + 5} r="1.6" fill="currentColor" stroke="none" /></>)
const Arcs = () => (<><path d="M9 11a18 18 0 0 0 0 26" /><path d="M39 11a18 18 0 0 1 0 26" /></>)

const ICONS = {
  oil: (<><path d="M6 21h6v-4h6v4h9l9-5h7l-14 14H10z" /><path d="M13 17v4" /><path d="M43 28c0 2-1.5 3.5-1.5 4.5a1.5 1.5 0 0 0 3 0C44.5 31.5 43 30 43 28z" fill="currentColor" stroke="none" /></>),
  coolant: (<><path d="M24 6v20" /><path d="M24 11h6M24 16h6M24 21h6" /><circle cx="24" cy="29" r="4" /><path d="M7 37c3-2 5-2 8 0s5 2 8 0 5-2 8 0 5 2 8 0 5-2 4 0" /><path d="M7 43c3-2 5-2 8 0s5 2 8 0 5-2 8 0 5 2 8 0" /></>),
  brake: (<><circle cx="24" cy="24" r="12" /><Arcs /><Bang y={17} h={8} /></>),
  battery: (<><rect x="6" y="15" width="36" height="22" rx="2" /><path d="M12 15v-4h6v4M30 15v-4h6v4" /><path d="M12 26h7M33 22.5v7M29.5 26h7" /></>),
  airbag: (<><circle cx="15" cy="10" r="3.5" /><path d="M15 16l-3 12h10l4 10" /><path d="M12 28l-4 9" /><circle cx="33" cy="22" r="9" /></>),
  eps: (<><circle cx="24" cy="24" r="14" /><circle cx="24" cy="24" r="4" /><path d="M10.5 22h9.5M28 22h9.5" /><path d="M42 12v8" /><circle cx="42" cy="25" r="1.6" fill="currentColor" stroke="none" /></>),
  'ev-system': (<><path d="M7 30l4-10h26l4 10v8H7z" /><circle cx="15" cy="38" r="3" /><circle cx="33" cy="38" r="3" /><Bang y={6} h={9} /></>),
  engine: (<><path d="M9 20h6v-4h12v4h5l3 4h4v-5h3v14h-3v-5h-4l-3 6H16l-3-4H9z" /><path d="M5 22v8M5 26h4" /></>),
  abs: (<><circle cx="24" cy="24" r="12" /><Arcs /><text {...T} x="24" y="28.5" fontSize="11">ABS</text></>),
  esp: (<><path d="M12 22l3-8h18l3 8v8H12z" /><circle cx="17" cy="30" r="2.5" /><circle cx="31" cy="30" r="2.5" /><path d="M14 38c2-3 4-3 6 0M26 38c2-3 4-3 6 0" /><path d="M10 44c3-3 6-3 9 0M29 44c3-3 6-3 9 0" /></>),
  tpms: (<><path d="M10 14c-3 6-3 16 0 22M38 14c3 6 3 16 0 22" /><path d="M10 36h28M13 40h4M22 40h4M31 40h4" /><Bang y={15} h={10} /></>),
  'adas-off': (<><path d="M9 26l3-8h16l3 8v7H9z" /><circle cx="14" cy="33" r="2.5" /><circle cx="26" cy="33" r="2.5" /><path d="M34 14a10 10 0 0 1 0 14M38 10a16 16 0 0 1 0 22" /><text {...T} x="24" y="46" fontSize="10">OFF</text></>),
  glow: (<><path d="M8 24c4-8 6-8 8 0s4 8 8 0 4-8 8 0 4 8 8 0" /><path d="M6 16h4M38 16h4" /></>),
  dpf: (<><rect x="10" y="12" width="28" height="24" rx="3" /><circle cx="18" cy="20" r="1.5" fill="currentColor" stroke="none" /><circle cx="24" cy="24" r="1.5" fill="currentColor" stroke="none" />
    <circle cx="30" cy="20" r="1.5" fill="currentColor" stroke="none" /><circle cx="18" cy="28" r="1.5" fill="currentColor" stroke="none" /><circle cx="30" cy="28" r="1.5" fill="currentColor" stroke="none" /><path d="M4 24h6M38 24h6" /></>),
  fuel: (<><rect x="10" y="9" width="18" height="30" rx="2" /><path d="M14 15h10v7H14z" /><path d="M28 18h5l4 4v13a2 2 0 0 1-4 0v-6h-5" /></>),
  seatbelt: (<><circle cx="22" cy="9" r="4" /><path d="M16 42V22a6 6 0 0 1 12 0v20" /><path d="M14 18l18 18" /><path d="M30 30v8" /></>),
}

export default function LightIcon({ id, size = 48 }) {
  return (
    <svg viewBox="0 0 48 48" width={size} height={size} aria-hidden="true" {...S}>{ICONS[id] || <Bang />}</svg>
  )
}
