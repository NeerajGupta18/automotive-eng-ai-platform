// What to do right now: shown before any troubleshooting when a description sounds dangerous.
export function Calls({ calls }) {
  return (
    <div className="calls">
      {calls.map((c) => (
        <a key={c.label} className="call" href={`tel:${c.number.replace(/[^\d+]/g, '')}`}>
          <span>{c.label}</span><strong>{c.number}</strong>
        </a>
      ))}
    </div>
  )
}

export default function Urgent({ card, onSafe }) {
  const stop = card.level === 'stop'
  return (
    <section className={`urgent ${stop ? 'stop' : 'accident'}`} role="alert" aria-labelledby="urgent-title">
      <h2 id="urgent-title">{card.title}</h2>
      <ol>{card.todo.map((t) => <li key={t}>{t}</li>)}</ol>
      <Calls calls={card.call} />
      {stop && onSafe && <button className="btn quiet block safe" onClick={onSafe}>I'm safe now, show help</button>}
    </section>
  )
}
