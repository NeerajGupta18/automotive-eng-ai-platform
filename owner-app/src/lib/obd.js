// OBD-II through an ELM327 Bluetooth LE adapter. The phone only sends commands and collects the text answers; the server
// decodes them (owner/obd.py), so this file stays small and every answer format is handled, and tested, in one place.
//
// Links: the Capacitor plugin in the Android app, Web Bluetooth in Chrome (Android and desktop) through the same plugin,
// and a demo adapter that answers like a real one. iPhones have no Web Bluetooth: the owner types the codes in instead.

// GATT services cheap ELM327 BLE adapters use (Vgate, Konnwei, "OBDII" HM-10 clones, Vlink); Web Bluetooth only shows
// the services listed here
export const SERVICES = [
  '0000fff0-0000-1000-8000-00805f9b34fb',
  '0000ffe0-0000-1000-8000-00805f9b34fb',
  '000018f0-0000-1000-8000-00805f9b34fb',
]
export const INIT = ['ATZ', 'ATE0', 'ATL0', 'ATS1', 'ATH0', 'ATSP0']
const enc = new TextEncoder()
const dec = new TextDecoder()

export function bluetoothAvailable() {
  const cap = window.Capacitor
  // in the Android app the native side lists its plugins in PluginHeaders before any plugin code is loaded
  const native = cap?.isNativePlatform?.() && (cap.PluginHeaders?.some((p) => p.name === 'BluetoothLe') || cap.isPluginAvailable?.('BluetoothLe'))
  return Boolean(native || navigator.bluetooth)
}

// Talks to an ELM327 over any link with write(text) and onData(callback). One command at a time; an answer ends at '>'.
export class Elm327 {
  constructor(link) {
    this.link = link
    this.buf = ''
    this.waiter = null
    this.stale = false
    link.onData((text) => this.feed(text))
  }

  feed(text) {
    this.buf += text
    let i
    while ((i = this.buf.indexOf('>')) >= 0) {
      const answer = this.buf.slice(0, i)
      this.buf = this.buf.slice(i + 1)
      if (this.stale) { this.stale = false; continue } // the late answer to a command that timed out
      const w = this.waiter
      this.waiter = null
      if (w) w(answer)
    }
  }

  async send(cmd, timeout = 6000) {
    if (this.stale) { await new Promise((r) => setTimeout(r, 300)); this.stale = false; this.buf = '' }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.waiter = null; this.stale = true; reject(new Error(`No answer to ${cmd}`)) }, timeout)
      this.waiter = (a) => { clearTimeout(timer); resolve(a) }
      this.link.write(`${cmd}\r`)
    })
  }

  async init(onStep) {
    for (const c of INIT) {
      onStep?.(c)
      await this.send(c, c === 'ATZ' ? 8000 : 4000)
    }
  }

  // Raw answers {command: text}; a command the car doesn't answer is recorded as NO DATA
  async scan(commands, onStep) {
    const answers = {}
    for (const c of commands) {
      onStep?.(c)
      try {
        answers[c] = await this.send(c, c === '0101' ? 15000 : 6000) // the first OBD request may search all protocols
      } catch {
        answers[c] = 'NO DATA'
      }
    }
    return answers
  }
}

// A real adapter over Bluetooth LE (Android app or Chrome)
export async function bleLink(onDisconnect) {
  const { BleClient } = await import('@capacitor-community/bluetooth-le')
  await BleClient.initialize({ androidNeverForLocation: true })
  const device = await BleClient.requestDevice({ optionalServices: SERVICES })
  await BleClient.connect(device.deviceId, () => onDisconnect?.())
  const services = await BleClient.getServices(device.deviceId)
  const usable = (s) => s.characteristics.some((c) => c.properties.notify || c.properties.indicate)
    && s.characteristics.some((c) => c.properties.write || c.properties.writeWithoutResponse)
  const svc = services.find((s) => SERVICES.includes(s.uuid.toLowerCase()) && usable(s)) || services.find(usable)
  if (!svc) {
    await BleClient.disconnect(device.deviceId)
    throw new Error("This Bluetooth device doesn't look like an OBD-II adapter.")
  }
  const notify = svc.characteristics.find((c) => c.properties.notify || c.properties.indicate)
  const write = svc.characteristics.find((c) => c.properties.writeWithoutResponse) || svc.characteristics.find((c) => c.properties.write)
  const listeners = []
  await BleClient.startNotifications(device.deviceId, svc.uuid, notify.uuid, (value) => {
    const text = dec.decode(new Uint8Array(value.buffer, value.byteOffset, value.byteLength))
    listeners.forEach((fn) => fn(text))
  })
  return {
    name: device.name || 'OBD-II adapter',
    onData: (fn) => listeners.push(fn),
    async write(text) {
      const bytes = enc.encode(text)
      for (let i = 0; i < bytes.length; i += 20) { // BLE packets are small
        const chunk = new DataView(bytes.slice(i, i + 20).buffer)
        if (write.properties.writeWithoutResponse) await BleClient.writeWithoutResponse(device.deviceId, svc.uuid, write.uuid, chunk)
        else await BleClient.write(device.deviceId, svc.uuid, write.uuid, chunk)
      }
    },
    async close() {
      try { await BleClient.stopNotifications(device.deviceId, svc.uuid, notify.uuid) } catch { /* already gone */ }
      try { await BleClient.disconnect(device.deviceId) } catch { /* already gone */ }
    },
  }
}

// The demo adapter: answers like an ELM327 in a 2023 petrol car with a lean-running engine (P0171) and a tired battery.
// It answers in pieces, with "SEARCHING..." and echo where a real adapter does, so the same code path is exercised.
const DEMO = {
  ATZ: '\r\rELM327 v1.5\r\r', ATE0: 'ATE0\rOK\r\r', ATL0: 'OK\r\r', ATS1: 'OK\r\r', ATH0: 'OK\r\r', ATSP0: 'OK\r\r',
  ATI: 'ELM327 v1.5\r\r', ATRV: '12.3V\r\r', ATDPN: 'A6\r\r', '0101': 'SEARCHING...\r41 01 81 07 E1 00\r\r', '03': '43 01 01 71\r\r',
  '07': '47 00\r\r', '0A': '4A 00\r\r', '0105': '41 05 46\r\r', '010C': '41 0C 00 00\r\r', '0142': '41 42 30 0C\r\r',
  '0121': '41 21 00 8C\r\r', '0131': '41 31 05 DC\r\r',
}

export function demoLink(answers = DEMO, delay = 120) {
  const listeners = []
  return {
    name: 'Demo adapter',
    onData: (fn) => listeners.push(fn),
    async write(text) {
      const cmd = text.trim().toUpperCase()
      const a = (answers[cmd] ?? '?\r\r') + '>'
      const cut = Math.ceil(a.length / 2)
      setTimeout(() => listeners.forEach((fn) => fn(a.slice(0, cut))), delay)
      setTimeout(() => listeners.forEach((fn) => fn(a.slice(cut))), delay * 2)
    },
    async close() {},
  }
}
