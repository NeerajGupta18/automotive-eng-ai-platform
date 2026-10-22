import { useEffect, useRef } from 'react'

// Bottom sheet for forms and details: Escape or the backdrop closes it, focus moves into it.
export default function Sheet({ title, onClose, children }) {
  const ref = useRef(null)
  useEffect(() => {
    const prev = document.activeElement
    const first = ref.current?.querySelector('input, select, textarea, button:not(.close)')
    ;(first || ref.current)?.focus()
    const key = (e) => e.key === 'Escape' && onClose()
    document.addEventListener('keydown', key)
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', key)
      document.body.style.overflow = ''
      prev && prev.focus && prev.focus()
    }
  }, [onClose])
  return (
    <div className="backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <section className="sheet" role="dialog" aria-modal="true" aria-label={title} ref={ref} tabIndex={-1}>
        <div className="grip" />
        <header>
          <h2>{title}</h2>
          <button className="close" onClick={onClose} aria-label="Close">×</button>
        </header>
        {children}
      </section>
    </div>
  )
}
