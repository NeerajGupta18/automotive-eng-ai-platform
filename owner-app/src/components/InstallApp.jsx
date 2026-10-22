import { useEffect, useState } from 'react'
import { canPrompt, inApp, installed, isAndroid, isIOS, onPromptChange, promptInstall } from '../lib/install.js'

// How to get the app on this device: the Android APK, the browser's install prompt, or Safari's steps.
export default function InstallApp({ android }) {
  const [, tick] = useState(0)
  useEffect(() => onPromptChange(() => tick((n) => n + 1)), [])
  if (inApp() || installed()) return null
  const items = []
  if (android && (isAndroid() || !isIOS())) {
    items.push(<a key="apk" className="btn ghost block" href="/get-app/android">Download the Android app</a>)
  }
  if (canPrompt()) {
    items.push(<button key="pwa" className="btn quiet block" onClick={() => promptInstall()}>Install on this device</button>)
  }
  if (isIOS()) {
    items.push(<p key="ios" className="small muted">On iPhone: open this page in Safari, tap Share, then Add to Home Screen.</p>)
  }
  if (!items.length) return <p className="small muted">Open this page on your phone to install it, or use your browser's install option.</p>
  return <div className="actions" style={{ flexDirection: 'column' }}>{items}</div>
}
