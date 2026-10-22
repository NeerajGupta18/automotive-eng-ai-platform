const P = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round', strokeLinejoin: 'round', viewBox: '0 0 24 24' }

export const InboxIcon = () => (<svg {...P} aria-hidden="true"><path d="M4 13.5 6.5 5h11l2.5 8.5V19H4z" /><path d="M4 13.5h4.5l1 2h5l1-2H20" /></svg>)
export const HomeIcon = () => (<svg {...P} aria-hidden="true"><path d="M3 11.5 12 4l9 7.5" /><path d="M5.5 10v9.5h13V10" /><path d="M10 19.5v-5h4v5" /></svg>)
export const CarIcon = () => (<svg {...P} aria-hidden="true"><path d="M4 16.5V12l2-5h12l2 5v4.5" /><path d="M3 12h18" /><circle cx="7.5" cy="16.5" r="1.8" /><circle cx="16.5" cy="16.5" r="1.8" /></svg>)
export const BellIcon = () => (<svg {...P} aria-hidden="true"><path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z" /><path d="M10 20.5a2 2 0 0 0 4 0" /></svg>)
export const UserIcon = () => (<svg {...P} aria-hidden="true"><circle cx="12" cy="8.5" r="3.5" /><path d="M5 20c1.2-3.5 4-5 7-5s5.8 1.5 7 5" /></svg>)
export const HelpIcon = () => (<svg {...P} aria-hidden="true"><circle cx="12" cy="12" r="8.5" /><circle cx="12" cy="12" r="3.5" /><path d="m6 6 3.5 3.5M18 6l-3.5 3.5M6 18l3.5-3.5M18 18l-3.5-3.5" /></svg>)

// help topics
const TOPIC = {
  bluetooth: <path d="m7 7.5 10 9-5 4v-17l5 4-10 9" />,
  music: <><path d="M9 17.5V6l10-2v11.5" /><circle cx="6.5" cy="17.5" r="2.5" /><circle cx="16.5" cy="15.5" r="2.5" /></>,
  phone: <><rect x="7" y="3" width="10" height="18" rx="2" /><path d="M10.5 17.5h3" /></>,
  screen: <><rect x="3" y="5" width="18" height="11" rx="1.5" /><path d="M9 20h6M12 16v4" /></>,
  camera: <><path d="M4 8h3l1.5-2h7L17 8h3v10H4z" /><circle cx="12" cy="13" r="3.2" /></>,
  radar: <><path d="M5 18h14" /><path d="M7 14.5a7 7 0 0 1 10 0" /><path d="M4.5 11.5a10.5 10.5 0 0 1 15 0" /><circle cx="12" cy="17" r="1" /></>,
  key: <><circle cx="8" cy="12" r="4" /><path d="M12 12h9M18 12v3M15.5 12v2" /></>,
  light: <><path d="M12 3.5 21 19H3z" /><path d="M12 9.5v4.5M12 16.6v.2" /></>,
}
export const TopicIcon = ({ name }) => (<svg {...P} aria-hidden="true" width="24" height="24">{TOPIC[name] || TOPIC.light}</svg>)
