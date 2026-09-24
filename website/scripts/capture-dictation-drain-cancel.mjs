/**
 * Recording harness for the way out of a released dictation's drain.
 *
 * Runs the REAL built SPA (website/dist) against a static file server with every
 * /api/** call answered from fixtures — no gateway, no token, no agent.
 *
 * ## What is real and what is not
 *
 * Two substitutions, both at the edge of the browser:
 *
 * 1. The capture DEVICE. `getUserMedia` returns a Web Audio MediaStream, because
 *    headless Chromium has no audio input and real `getUserMedia({audio:true})`
 *    rejects with NotSupportedError even with `--use-fake-device-for-media-stream`.
 *    Same substitution, and the same reason, as `capture-dictation-panel.mjs`.
 * 2. The RECOGNISER. `/api/ws/stt` is routed in-page: it answers the handshake,
 *    accepts PCM, and then answers the client's `stop` with nothing at all. That
 *    silence IS the condition under test — a recogniser that must fetch and load
 *    its weights before it can decode holds the released utterance for minutes,
 *    and the browser waits in `draining` for the whole of it.
 *
 * Everything between them is the unmodified production path: `useVoiceInput`,
 * `useStreamingStt`, `useComposerVoice` and `ChatInput` as shipped.
 *
 * Records video because the subject is a transition, not a layout: the frames
 * differ by whether one keystroke is answered.
 *
 * Usage: node scripts/capture-dictation-drain-cancel.mjs [outDir] [label]
 *
 * `label` prefixes the file names, so the same harness run against a checkout
 * without the fix produces the comparison half ("before").
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json } from './lib/boot-api.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/dictation-drain-cancel'
const LABEL = process.argv[3] || 'after'
const SLOT = 'chat-drain'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Dictation drain',
  running: false,
  last_message: '',
  messages: 0,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = { running: false, has_more: false, total: 0, queue: [], project: PROJECT, messages: [] }

/** A silent-but-live microphone: the level meter has a real stream to read and
 *  the streaming path has real PCM to send. Shape does not matter here — the
 *  subject is the wait after the release, not the waveform during capture. */
function installSyntheticMic() {
  const AC = window.AudioContext || window.webkitAudioContext
  const ctx = new AC()
  const dest = ctx.createMediaStreamDestination()
  const osc = ctx.createOscillator()
  const gain = ctx.createGain()
  osc.type = 'sawtooth'
  osc.frequency.value = 180
  gain.gain.value = 0.2
  osc.connect(gain).connect(dest)
  osc.start()
  const stream = dest.stream
  try {
    Object.defineProperty(stream.getAudioTracks()[0], 'label', { value: 'Synthetic Test Microphone' })
  } catch { /* label stays empty; the status row omits it */ }
  navigator.mediaDevices.getUserMedia = async () => stream
  navigator.mediaDevices.enumerateDevices = async () => [
    { kind: 'audioinput', deviceId: 'synthetic', label: 'Synthetic Test Microphone', groupId: 'g' },
  ]

  /** `voiceInputSupported` asks `MediaRecorder.isTypeSupported` before the mic
   *  button renders at all, and headless Chromium ships no audio encoder, so
   *  without this the composer has no voice control to press. The streaming path
   *  never constructs a recorder — PCM goes through the worklet — so a no-op
   *  recorder only restores the support probe. */
  class NoopRecorder {
    static isTypeSupported() { return true }
    constructor(s) { this.stream = s; this.state = 'inactive'; this.ondataavailable = null; this.onstop = null }
    start() { this.state = 'recording' }
    stop() { this.state = 'inactive'; this.onstop?.() }
  }
  window.MediaRecorder = NoopRecorder
}

const { srv, base } = await serveDist()

// The GL flags keep the dictation panel's shader strands rendering in headless
// Chromium; the panel is up for the capture half of the run.
const browser = await chromium.launch({
  args: ['--use-gl=angle', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'],
})
const context = await browser.newContext({
  viewport: { width: 1280, height: 820 },
  deviceScaleFactor: 2,
  permissions: ['microphone'],
  recordVideo: { dir: OUT, size: { width: 1280, height: 820 } },
})
const page = await context.newPage()
const errors = []
page.on('pageerror', e => errors.push(`PAGEERROR: ${e.message}`))
page.on('console', m => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)) })

// Boot fixtures, the dashboard socket and the localStorage seeds come from the
// shared stub; only the two endpoints this capture actually depends on are named
// here. Each branch AWAITS `json()` and then returns true, because the stub reads
// a falsy return as "not handled" and would fulfil the route a second time.
await stubDashboardApi(page, {
  slots,
  localStorageEntries: { 'mc-active-slot-chat': SLOT },
  extra: async (path, route) => {
    if (path === '/api/config/stt') {
      await json(route, {
        enabled: true, dictation_panel: true, streaming: true,
        provider: 'whisper', available: true,
        model: 'turbo', models: { turbo: '809M' }, language_code: 'en-US',
        install_step: '', install_detail: '', install_error: '', prereqs: [],
      })
      return true
    }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  },
})

/** The recogniser that accepts the session and then never delivers.
 *
 *  Bound AFTER the shared stub, which swallows `/api/ws` wholesale: the last
 *  matching route wins, so registering this one later is what lets the
 *  recogniser's own path reach this handler instead of that catch-all. */
let sttFrames = 0
let sttConnections = 0
const sttControl = []
await page.routeWebSocket(/\/api\/ws\/stt/, ws => {
  sttConnections++
  // `ready` arrives unprompted, exactly as the real endpoint emits it before any
  // provider branch runs — the client sends no control frame to ask for it, only
  // binary audio and one `stop`. Sending it is what makes this the wait being
  // photographed: the recogniser has accepted the session and owes a final
  // transcript, so the release enters the long final wait rather than the short
  // pre-ready path that gives up with a connection error of its own accord.
  ws.send(JSON.stringify({ type: 'ready', final_timeout_ms: 315000 }))
  ws.onMessage(msg => {
    if (typeof msg === 'string') {
      // The one control frame the client does send: `stop`, at release. Record it
      // and stay silent — no partial, no final, no error.
      sttControl.push(msg.slice(0, 120))
      return
    }
    sttFrames++
  })
})

await page.addInitScript(installSyntheticMic)

await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })

const composer = page.getByLabel('Message input')
await composer.waitFor({ timeout: 20000 })
await composer.fill('a draft the user typed before dictating')

/** The composer's voice control. Located by the union of its three names,
 *  because the same button is "Voice input", then "Stop recording", then
 *  "Transcribing": a locator naming only the first stops matching it exactly
 *  when the run needs it most. Scoped to the composer so the union cannot pick
 *  up a similarly-named control elsewhere on the page. */
const mic = page.getByTestId('input-wrapper').getByRole('button', { name: /voice input|stop recording|transcribing/i }).first()

/** Fail loudly and usefully: a missing voice control means the browser refused
 *  the support probe, not that the change is wrong, and the two are impossible
 *  to tell apart from a bare locator timeout. */
async function requireMic() {
  if (await mic.count() === 0) {
    const names = await page.evaluate(() =>
      Array.from(document.querySelectorAll('button')).map(b => b.getAttribute('aria-label') || b.textContent?.trim().slice(0, 40)).filter(Boolean))
    throw new Error(`no voice control in the composer. page errors: ${JSON.stringify(errors.slice(0, 4))}; buttons: ${JSON.stringify(names)}`)
  }
  return mic
}

/** Two frames per beat: the whole window for context, and the composer alone,
 *  because what changes is one 18px control and a full-page frame renders it too
 *  small to read on a pull request. */
async function shots(beat) {
  await page.screenshot({ path: `${OUT}/${LABEL}-${beat}.png` })
  await page.getByTestId('input-wrapper').screenshot({ path: `${OUT}/${LABEL}-composer-${beat}.png` })
}

// Dictate, then release: the mic button is the commit, so pressing it twice is
// exactly the gesture a user makes.
await requireMic()
const idleTitle = await mic.getAttribute('title')
await mic.click()
await page.getByTestId('voice-dictation-panel').waitFor({ timeout: 15000 })
await page.waitForTimeout(1500)
await shots('1-recording')

await mic.click()
// Capture has ended and the panel is down; the utterance is now queued behind a
// recogniser that never answers. This is the trapped composer from the issue.
await page.getByTestId('voice-dictation-panel').waitFor({ state: 'detached', timeout: 10000 })
await page.waitForTimeout(1800)
await shots('2-drain-locked')

// The run is only evidence if the STREAMING path ran: the batch path reaches a
// similar-looking locked mic through `transcribing`, which this change does not
// touch, and the two are indistinguishable in a screenshot. The socket is the
// discriminator — batch never opens one.
if (sttConnections === 0) throw new Error('no /api/ws/stt connection: this run took the batch path, not the drain under test')

const lockedTitle = await mic.getAttribute('title')
const lockedDisabled = await mic.isDisabled()

// The keystroke under test.
await page.keyboard.press('Escape')
await page.waitForTimeout(1800)
await shots('3-after-escape')

const afterTitle = await mic.getAttribute('title')
const afterDisabled = await mic.isDisabled()

await page.waitForTimeout(600)
const video = page.video()
await context.close()
if (video) await video.saveAs(`${OUT}/${LABEL}-drain-cancel.webm`)
await browser.close()
srv.close()

console.log(JSON.stringify({
  out: OUT,
  label: LABEL,
  sttConnections,
  pcmFramesSent: sttFrames,
  sttControlFrames: sttControl,
  micIdle: { title: idleTitle },
  micWhileDraining: { title: lockedTitle, disabled: lockedDisabled },
  micAfterEscape: { title: afterTitle, disabled: afterDisabled },
  freedByEscape: lockedDisabled === true && afterDisabled === false,
  errors,
}, null, 2))
if (errors.length) process.exitCode = 1
