// The ELM327 driver against the demo adapter and against links that misbehave (split answers, silence, late answers).
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { demoLink, Elm327, INIT } from '../src/lib/obd.js'

const COMMANDS = ['ATI', 'ATRV', 'ATDPN', '0101', '03', '07', '0A', '0105', '010C', '0142', '0121', '0131']

test('initialises and collects one answer per command from the demo adapter', async () => {
  const elm = new Elm327(demoLink(undefined, 2))
  const seen = []
  await elm.init((c) => seen.push(c))
  assert.deepEqual(seen, INIT)
  const answers = await elm.scan(COMMANDS)
  assert.deepEqual(Object.keys(answers), COMMANDS)
  assert.match(answers['0101'], /SEARCHING\.\.\.\r41 01 81 07 E1 00/)
  assert.equal(answers['03'].trim(), '43 01 01 71')
  assert.equal(answers.ATRV.trim(), '12.3V')
})

test('an answer split across many notifications is put back together', async () => {
  const listeners = []
  const link = {
    onData: (fn) => listeners.push(fn),
    write: () => { for (const ch of '41 05 46\r\r>') setTimeout(() => listeners.forEach((fn) => fn(ch)), 1) },
  }
  assert.equal((await new Elm327(link).send('0105')).trim(), '41 05 46')
})

test('a command without an answer becomes NO DATA, and its late answer is not given to the next command', async () => {
  const listeners = []
  let n = 0
  const link = {
    onData: (fn) => listeners.push(fn),
    write: (text) => {
      n += 1
      if (text.startsWith('0142')) setTimeout(() => listeners.forEach((fn) => fn('41 42 30 0C\r>')), 60) // too late
      else setTimeout(() => listeners.forEach((fn) => fn(`ANSWER ${text.trim()}\r>`)), 5)
    },
  }
  const elm = new Elm327(link)
  const p = elm.send('0142', 30)
  await assert.rejects(p, /No answer to 0142/)
  await new Promise((r) => setTimeout(r, 80)) // the late answer arrives and is dropped
  assert.equal((await elm.send('0105')).trim(), 'ANSWER 0105')
  assert.equal(n, 2)
})

test('scan never throws: a silent car gives NO DATA for every command', async () => {
  const elm = new Elm327({ onData: () => {}, write: () => {} })
  elm.send = (cmd) => Promise.reject(new Error(`No answer to ${cmd}`))
  assert.deepEqual(await elm.scan(['0105', '03']), { '0105': 'NO DATA', '03': 'NO DATA' })
})
