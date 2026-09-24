/**
 * Screenshot harness for the inline-code chip cue.
 *
 * Proves, from the live DOM before any pixel is taken:
 *  1. At rest, a click-to-copy chip wears code styling (no accent colour, no
 *     underline on hover, the `copy` cursor) while a backend-confirmed path chip
 *     keeps the actionable look (accent, hover underline, pointer, glyph) — and
 *     each chip's accessible name states ITS click ("Copy …" vs "Open …"). The
 *     colours are asserted as COMPUTED values on both Kiro themes: the theme's
 *     inline-code rule outranks the `text-accent` utility, so only a chip rule
 *     keyed on `data-chip-action` makes the accent actually paint.
 *  2. Hovering the copy chip shows the tooltip that names the action.
 *  3. Clicking it copies, flips the tooltip to "Copied!", and fills the sr-only
 *     status region with the same word; the bubble survives the pointer leaving
 *     and closes by itself when the flash ends.
 *  4. With both clipboard layers stubbed to refuse, the click renders the
 *     "Copy failed" notice in the SAME bubble (an ErrorNotice, announced once),
 *     claims nothing, and moves nothing in the paragraph; it too outlives the
 *     leave and clears itself.
 *  5. A long path chip still breaks across lines (measured: more than one
 *     client rect), and a copy on a long copy chip moves nothing on the line —
 *     the text after it has the same box before and after the confirmation.
 *
 * Paths are probed through the same `/api/file-read` HEAD request production
 * issues; a route registered AFTER the harness's catch-all answers it with the
 * `X-Path-Kind` header (Playwright matches newest-first).
 *
 * Usage: node scripts/capture-inline-code-chip-cue.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'

// The node toolchain injects its own libstdc++ on LD_LIBRARY_PATH, which the
// bundled Chromium then loads in preference to the system one and fails on.
delete process.env.LD_LIBRARY_PATH

const { openTranscriptHarness } = await import('./lib/transcript-harness.mjs')

const OUT = process.argv[2] || '../temp-screenshots/inline-code-chip-cue'
const SLOT = 'chat-chipcue'
// A synthetic project: the value renders into the frame, so it must not be a
// real checkout path.
const PROJECT = '/home/user/project'

mkdirSync(OUT, { recursive: true })

const FILE = '/home/user/project/website/vitest.config.mts'
const DIR = '/home/user/project/website/src/test'
const LONG_FILE = '/home/user/project/website/src/components/feature/subfeature/another/level/AnExtremelyLongComponentFileNameThatKeepsGoingAndGoing.tsx'
const LONG_COPY = 'SOME_VERY_LONG_ENVIRONMENT_VARIABLE_NAME_THAT_WRAPS_THE_LINE=/opt/some/deeply/nested/config/path/for/the/gateway/service/settings.toml'
/** What the stubbed backend confirms. Anything else answers 404. */
const KINDS = { [FILE]: 'file', [DIR]: 'dir', [LONG_FILE]: 'file' }

const now = Date.now() / 1000
const slots = [{
  key: SLOT, title: 'Inline code chips', running: false,
  last_message: 'where the tests live', messages: 4, agent: 'kirocrew',
  memory_mode: 'persistent', project: PROJECT, modified: Math.floor(now),
  source_links: [], source_links_total: 0,
}]
const detail = {
  running: false, has_more: false, total: 4, queue: [], project: PROJECT,
  messages: [
    { role: 'user', ts: now - 900, content: 'Where do the frontend tests live, and how do I run them?' },
    {
      role: 'assistant', ts: now - 850, content: [
        `Run \`npm test\` from the website directory. The runner config is \`${FILE}\` and the fixtures sit under \`${DIR}\`.`,
        '',
        'Set `NODE_ENV=production` before `npm run build` to get the shipped bundle.',
      ].join('\n'),
    },
    { role: 'user', ts: now - 500, content: 'And the long ones?' },
    {
      role: 'assistant', ts: now - 30, content: [
        `The deepest one is \`${LONG_FILE}\` — it wraps like any other inline code.`,
        '',
        `The gateway reads \`${LONG_COPY}\` at boot, then falls back to defaults.`,
      ].join('\n'),
    },
  ],
}

async function main() {
  const h = await openTranscriptHarness({
    slot: SLOT, project: PROJECT, slots, detail,
    viewport: { width: 1180, height: 900 },
  })
  await h.page.context().grantPermissions(['clipboard-read', 'clipboard-write'], { origin: h.base })

  // Newest route wins: answer the path probe with the confirmed kinds.
  await h.page.route('**/api/file-read**', route => {
    const url = new URL(route.request().url())
    const kind = KINDS[url.searchParams.get('path') ?? '']
    if (!kind) return route.fulfill({ status: 404, body: '' })
    return route.fulfill({ status: 200, headers: { 'X-Path-Kind': kind }, body: '' })
  })

  let failures = 0
  const assert = (label, ok) => {
    console.log(`${ok ? 'PASS' : 'FAIL'}: ${label}`)
    if (!ok) failures += 1
  }
  /** Clip the frame to `locator`'s box, grown by `pad` so a bubble above it stays in. */
  const shotAround = async (locator, name, pad = 48) => {
    const box = await locator.boundingBox()
    const clip = {
      x: Math.max(0, box.x - 16), y: Math.max(0, box.y - pad),
      width: Math.min(1180 - Math.max(0, box.x - 16), box.width + 32), height: box.height + pad + 16,
    }
    await h.page.screenshot({ path: `${OUT}/${name}.png`, clip })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  const copyChip = () => h.page.locator('code[aria-label="Copy npm test"]')
  const fileChip = () => h.page.locator(`code[data-path="${FILE}"]`)
  const dirChip = () => h.page.locator(`code[data-path="${DIR}"]`)
  const firstMsg = () => h.page.locator('[data-role="assistant"] .msg-content').first()
  const lastMsg = () => h.page.locator('[data-role="assistant"] .msg-content').last()

  for (const theme of ['dark', 'light']) {
    await h.load(theme, { selector: 'textarea[data-composer-input]', settle: 900 })
    await fileChip().waitFor({ timeout: 10_000 })
    await dirChip().waitFor({ timeout: 10_000 })

    const rest = await h.page.evaluate(([copySel, fileSel, dirSel]) => {
      const read = sel => {
        const el = document.querySelector(sel)
        const cs = getComputedStyle(el)
        return {
          name: el.getAttribute('aria-label'), cls: el.className, cursor: cs.cursor,
          display: cs.display, color: cs.color, glyph: !!el.querySelector('svg:not([class*="opacity-0"])'),
          title: el.getAttribute('title'), action: el.getAttribute('data-chip-action'),
        }
      }
      return { copy: read(copySel), file: read(fileSel), dir: read(dirSel) }
    }, ['code[aria-label="Copy npm test"]', `code[data-path="${FILE}"]`, `code[data-path="${DIR}"]`])

    assert(`${theme}: copy chip is named for its click (${rest.copy.name})`, rest.copy.name === 'Copy npm test')
    assert(`${theme}: file chip is named for its click (${rest.file.name})`, rest.file.name === `Open ${FILE}`)
    assert(`${theme}: dir chip is named for its click (${rest.dir.name})`, rest.dir.name === `Browse ${DIR}`)
    assert(`${theme}: copy chip cursor is "copy" (${rest.copy.cursor})`, rest.copy.cursor === 'copy')
    assert(`${theme}: path chips cursor is "pointer" (${rest.file.cursor}, ${rest.dir.cursor})`,
      rest.file.cursor === 'pointer' && rest.dir.cursor === 'pointer')
    assert(`${theme}: copy chip carries no accent colour / underline classes`,
      !/text-accent|underline/.test(rest.copy.cls))
    assert(`${theme}: path chips carry the actionable classes and a visible glyph`,
      /text-accent/.test(rest.file.cls) && /hover:underline/.test(rest.file.cls) && rest.file.glyph && rest.dir.glyph)
    // The COMPUTED colour, not the class: on the shipped default theme (Kiro)
    // index.css paints every inline code span (`[data-theme="kiro-*"] .msg-content
    // :not(pre)>code{color:…}`, specificity 0,2,2) and the unlayered `text-accent`
    // utility (0,1,0) loses to it, so the class alone painted nothing — every chip
    // wore the code colour. The actionable chips get their own rule, keyed on
    // `data-chip-action`; the copy chip's look IS the code colour.
    const CODE_COLOUR = { dark: 'rgb(193, 154, 255)', light: 'rgb(114, 58, 204)' } // #c19aff / #723acc
    const ACCENT_COLOUR = { dark: 'rgb(176, 127, 255)', light: 'rgb(142, 72, 255)' } // #b07fff (message links) / --accent
    assert(`${theme}: chips declare their action (${rest.copy.action} / ${rest.file.action} / ${rest.dir.action})`,
      rest.copy.action === 'copy' && rest.file.action === 'open' && rest.dir.action === 'browse')
    assert(`${theme}: copy chip paints in the theme's inline-code colour (${rest.copy.color})`,
      rest.copy.color === CODE_COLOUR[theme])
    assert(`${theme}: path chips paint in the accent (${rest.file.color}, ${rest.dir.color})`,
      rest.file.color === ACCENT_COLOUR[theme] && rest.dir.color === ACCENT_COLOUR[theme])
    assert(`${theme}: copy and path chips differ in colour (${rest.copy.color} vs ${rest.file.color})`,
      rest.copy.color !== rest.file.color)
    assert(`${theme}: copy chip has no glyph and no native title`, !rest.copy.glyph && rest.copy.title === null)
    assert(`${theme}: file chip title names the open action`, /Click to open/.test(rest.file.title ?? ''))
    assert(`${theme}: every chip is a plain inline box (${rest.copy.display} / ${rest.file.display})`,
      rest.copy.display === 'inline' && rest.file.display === 'inline' && rest.dir.display === 'inline')

    await shotAround(firstMsg(), `rest-${theme}`, 16)
  }

  // --- Hover: the tooltip names the action; no underline appears. ---
  await copyChip().hover()
  await h.page.waitForTimeout(300)
  const hoverTip = await h.page.locator('[role="tooltip"]').textContent().catch(() => null)
  assert(`hover: tooltip reads "Click to copy" (${hoverTip})`, hoverTip === 'Click to copy')
  const hoverDeco = await copyChip().evaluate(el => getComputedStyle(el).textDecorationLine)
  assert(`hover: copy chip stays un-underlined (${hoverDeco})`, hoverDeco === 'none')
  const describedBy = await copyChip().getAttribute('aria-describedby')
  const tipId = await h.page.locator('[role="tooltip"]').getAttribute('id')
  assert('hover: tooltip is the chip\'s accessible description', !!describedBy && describedBy === tipId)
  await shotAround(firstMsg(), 'hover-tooltip')

  // Contrast: hovering the path chip underlines it — the link look it keeps.
  await fileChip().hover()
  await h.page.waitForTimeout(150)
  const pathDeco = await fileChip().evaluate(el => getComputedStyle(el).textDecorationLine)
  assert(`hover: path chip underlines (${pathDeco})`, pathDeco === 'underline')
  assert('hover: path chip shows no instant bubble (native title only)', (await h.page.locator('[role="tooltip"]').count()) === 0)

  // --- Click: copies, confirms in the bubble and the status region, clears. ---
  // The status region is a sibling INSIDE the message DOM (out of flow, sr-only),
  // so the message's textContent legitimately gains "Copied!"; what must not
  // change is the chip's own text and the paragraph's box.
  const chipTextBefore = await copyChip().evaluate(el => el.textContent)
  const paraBefore = await firstMsg().evaluate(el => JSON.stringify(el.querySelector('p').getBoundingClientRect()))
  const copyBoxBefore = await copyChip().boundingBox()
  await copyChip().hover()
  await h.page.waitForTimeout(200)
  await copyChip().click()
  await h.page.waitForTimeout(250)
  const clipboard = await h.page.evaluate(() => navigator.clipboard.readText()).catch(() => null)
  assert(`click: clipboard holds the chip text (${JSON.stringify(clipboard)})`, clipboard === 'npm test')
  const copiedTip = await h.page.locator('[role="tooltip"]').textContent().catch(() => null)
  assert(`click: tooltip flips to "Copied!" (${copiedTip})`, copiedTip === 'Copied!')
  const status = await h.page.locator('[role="status"]').filter({ hasText: 'Copied!' }).count()
  assert(`click: one status region announces "Copied!" (${status})`, status === 1)
  const chipTextAfter = await copyChip().evaluate(el => el.textContent)
  const paraAfter = await firstMsg().evaluate(el => JSON.stringify(el.querySelector('p').getBoundingClientRect()))
  const copyBoxAfter = await copyChip().boundingBox()
  assert('click: nothing was appended inside the chip', chipTextAfter === chipTextBefore)
  assert('click: the paragraph box did not change', paraAfter === paraBefore)
  assert('click: the chip box did not change', JSON.stringify(copyBoxBefore) === JSON.stringify(copyBoxAfter))
  await shotAround(firstMsg(), 'copied-confirmation')
  // The pointer moves on right after the click, as a mouse user does: the
  // confirmation must survive the leave and close on its own when the flash ends.
  await h.page.mouse.move(5, 5)
  await h.page.waitForTimeout(100)
  const leftTip = await h.page.locator('[role="tooltip"]').textContent().catch(() => null)
  assert(`click: "Copied!" survives the pointer leaving (${leftTip})`, leftTip === 'Copied!')
  await h.page.waitForTimeout(1600)
  assert('click: the bubble closed by itself once the flash ended', (await h.page.locator('[role="tooltip"]').count()) === 0)
  assert('click: status region is empty again',
    (await h.page.locator('[role="status"]').filter({ hasText: 'Copied!' }).count()) === 0)
  // Hovering again shows the prompt, not a stale outcome.
  await copyChip().hover()
  await h.page.waitForTimeout(300)
  const clearedTip = await h.page.locator('[role="tooltip"]').textContent().catch(() => null)
  assert(`click: tooltip is back to "Click to copy" (${clearedTip})`, clearedTip === 'Click to copy')
  await h.page.mouse.move(5, 5)
  await h.page.waitForTimeout(100)

  // --- Refused write: both clipboard layers say no -> the failure is rendered. ---
  // The async Clipboard API rejects (a refused `clipboard-write` permission) and
  // the execCommand fallback reports false, which is the shape a sandboxed or
  // permission-denied document produces; `copyToClipboard` then resolves false.
  await h.page.evaluate(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: () => Promise.reject(new DOMException('denied', 'NotAllowedError')) },
    })
    document.execCommand = () => false
  })
  const envChip = h.page.locator('code[aria-label="Copy NODE_ENV=production"]')
  const envParaBefore = await envChip.evaluate(el => JSON.stringify(el.closest('p').getBoundingClientRect()))
  await envChip.hover()
  await h.page.waitForTimeout(200)
  await envChip.click()
  await h.page.waitForTimeout(300)
  // Same place as the confirmation: the bubble, through ErrorNotice. Nothing in
  // the text flow, so the paragraph's box is byte-identical.
  const failNotice = h.page.locator('[role="tooltip"] [data-testid="md-chip-copy-error"]')
  assert(`refused: exactly one "Copy failed" notice renders, inside the bubble (${await failNotice.count()})`, (await failNotice.count()) === 1)
  assert('refused: the notice reads "Copy failed"', /Copy failed/.test((await failNotice.textContent()) ?? ''))
  const bubbleAlerts = await h.page.locator('[role="tooltip"] [role="alert"]').count()
  assert(`refused: the notice is the bubble's one alert, announced once (${await failNotice.getAttribute('role')}, ${bubbleAlerts})`,
    (await failNotice.getAttribute('role')) === 'alert' && bubbleAlerts === 1)
  assert('refused: nothing entered the paragraph',
    await envChip.evaluate(el => el.closest('p').querySelector('[data-testid="md-chip-copy-error"], [role="alert"]') === null))
  const envParaAfter = await envChip.evaluate(el => JSON.stringify(el.closest('p').getBoundingClientRect()))
  assert('refused: the paragraph box did not change', envParaBefore === envParaAfter)
  const refusedTip = await h.page.locator('[role="tooltip"]').textContent().catch(() => null)
  assert(`refused: tooltip never claims "Copied!" (${refusedTip})`, !!refusedTip && !/Copied!/.test(refusedTip))
  assert('refused: nothing announced as copied',
    (await h.page.locator('[role="status"]').filter({ hasText: 'Copied!' }).count()) === 0)
  await shotAround(firstMsg(), 'copy-failed-notice')
  // The failure holds the bubble through the leave too, then clears itself.
  await h.page.mouse.move(5, 5)
  await h.page.waitForTimeout(100)
  assert('refused: the failure survives the pointer leaving', (await failNotice.count()) === 1)
  await h.page.waitForTimeout(3100)
  assert('refused: the failure cleared itself and the bubble closed', (await h.page.locator('[role="tooltip"]').count()) === 0)
  // Restore the real clipboard for the long-chip scene below.
  await h.page.reload({ waitUntil: 'domcontentloaded' })
  await h.page.waitForSelector('textarea[data-composer-input]', { timeout: 20000 })
  await h.page.waitForTimeout(900)

  // --- Long chips: still wrap; a copy moves nothing. ---
  const longFile = h.page.locator(`code[data-path="${LONG_FILE}"]`)
  await longFile.waitFor({ timeout: 10_000 })
  const longCopy = h.page.locator(`code[aria-label="Copy ${LONG_COPY}"]`)
  await lastMsg().scrollIntoViewIfNeeded()
  const rects = await longFile.evaluate(el => el.getClientRects().length)
  assert(`wrap: the long path chip spans ${rects} line boxes (must be > 1)`, rects > 1)
  const copyRects = await longCopy.evaluate(el => el.getClientRects().length)
  assert(`wrap: the long copy chip spans ${copyRects} line boxes (must be > 1)`, copyRects > 1)
  // The prose right after the long copy chip: its box must not move when the
  // confirmation shows — that is what "non-layout" means in pixels.
  const tailBefore = await lastMsg().evaluate(el => {
    const p = el.querySelectorAll('p')[1]
    const r = p.getBoundingClientRect()
    return { h: r.height, bottom: r.bottom }
  })
  await longCopy.hover()
  await h.page.waitForTimeout(200)
  await longCopy.click()
  await h.page.waitForTimeout(250)
  const longTip = await h.page.locator('[role="tooltip"]').textContent().catch(() => null)
  assert(`wrap: long copy chip confirms in the bubble (${longTip})`, longTip === 'Copied!')
  const tailAfter = await lastMsg().evaluate(el => {
    const p = el.querySelectorAll('p')[1]
    const r = p.getBoundingClientRect()
    return { h: r.height, bottom: r.bottom }
  })
  assert(`wrap: paragraph box unchanged by the confirmation (${JSON.stringify(tailBefore)} -> ${JSON.stringify(tailAfter)})`,
    tailBefore.h === tailAfter.h && tailBefore.bottom === tailAfter.bottom)
  const copyRectsAfter = await longCopy.evaluate(el => el.getClientRects().length)
  assert(`wrap: long copy chip still spans ${copyRectsAfter} line boxes while confirmed`, copyRectsAfter === copyRects)
  await shotAround(lastMsg(), 'long-path-wraps')

  await h.close()
  if (failures > 0) {
    console.error(`${failures} assertion(s) failed`)
    process.exit(1)
  }
}

await main()
