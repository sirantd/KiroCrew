/**
 * Screenshot harness for the workspace tree's STATE ROW under a childless
 * folder (#13054): "Empty folder", "Contains only hidden items", "Files not
 * shown: file limit of 10,000 reached" -- for the truncation badge that yields
 * to its own state row while the folder is expanded, for the folder the server
 * could not read (no state row and no marker on its row: a "Folders not
 * readable" alert with the agent hand-off above the tree), for the two root
 * states that replace the empty-workspace notice when the server could not
 * read the project root itself or the root holds only hidden items, and for
 * the listing-failure notice that replaces the endless shimmer when the first
 * `/api/project/tree` request fails.
 *
 * Same house pattern as `capture-pierre-files-tab.mjs`: the REAL built SPA
 * behind the shared in-process static server, every `/api/**` answered from
 * fixtures via Playwright route interception -- gateway-free. The client code
 * under test is unmodified. Run it once against a dist built from `main` and
 * once against the branch's dist; the fixture and the clicks are identical, so
 * the two frames differ only by what the tree paints under an expanded folder.
 *
 * Fixture: the reporter's shape. A NON-repository workspace whose `_bg/` holds
 * only a hidden `.kiro/` folder (`hiddenOnlyDirectories`), an `empty/` folder
 * that is empty on disk, a `big/` folder whose files fell to the file cap
 * (`truncatedDirectories`), a `vault/` folder whose only entry `locked/` the
 * server could not read (`unreadableDirectories` -- the folder is listed as an
 * ordinary row, nothing is claimed beneath it, and `vault/` itself is not called
 * empty), and a populated `src/`. A dist built before this fix ignores the
 * qualifier lists and paints nothing under any of them.
 *
 * Frames (`<prefix>` is the --prefix argument, e.g. `before` / `after`):
 *   <prefix>-10-folders-expanded   `_bg`, `empty`, `big`, `vault`, `vault/locked`
 *                                  and `src` expanded; `big`'s badge has
 *                                  yielded to the state row beneath it; the
 *                                  "Folders not readable: vault/locked" alert
 *                                  with the agent hand-off sits above the tree
 *   <prefix>-11-bg-collapsed       `_bg` and `big` collapsed again (their rows
 *                                  must go; `big`'s "files not shown" badge is
 *                                  back on the closed folder)
 *   <prefix>-20-listing-failed     the tree request answers 503: the HOST's
 *                                  own notice + Refresh (not changed by this
 *                                  PR; captured to show the failure path is
 *                                  not silent)
 *   <prefix>-30-root-unreadable    the server names the project root itself
 *                                  (`.`) as unreadable: a notice naming the
 *                                  folder ("Couldn't read the folder <path>")
 *                                  with Refresh and the agent hand-off in the
 *                                  tree's place, not "No files in this
 *                                  workspace yet"
 *   <prefix>-31-root-hidden-only   the server names the project root (`.`) as
 *                                  hidden-only: its top level holds only
 *                                  items the listing hides, so "This workspace
 *                                  contains only hidden items" stands in the
 *                                  tree's place, not the empty-workspace notice
 *
 * Every frame is gated: the surface it names must have demonstrably rendered
 * (shadow-piercing probes, then a bytes-per-pixel blank check).
 *
 * Usage: node scripts/capture-files-tree-empty-folder.mjs <outDir> --prefix <p> [--dist <path>] [--expect-state-rows]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const args = process.argv.slice(2)
const flag = (name) => { const i = args.indexOf(name); return i === -1 ? null : args[i + 1] }
const FLAG_VALUES = new Set(['--prefix', '--dist'].map(f => flag(f)).filter(Boolean))
const OUT = args.find(a => !a.startsWith('--') && !FLAG_VALUES.has(a)) || '../temp-screenshots/files-tree-empty-folder'
const PREFIX = flag('--prefix') || 'after'
const DIST = flag('--dist') ? resolve(flag('--dist')) : DEFAULT_DIST
/** With the flag, the state rows MUST render (the fix is in the dist); without
 *  it they MUST NOT (a pre-fix dist) -- the BEFORE frame is only evidence if it
 *  proves the absence it claims. */
const EXPECT_STATE_ROWS = args.includes('--expect-state-rows')

const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const SLOT = 'chat-files-tree-empty'
const MAX_EDGE = 2000
const MIN_MBPP = 15

mkdirSync(OUT, { recursive: true })

// ── Fixtures ────────────────────────────────────────────────────────────────

const TREE = {
  root: PROJECT,
  repo: false,
  // `trailing-marker\u200b` is a REAL file whose name ends in the state-row
  // marker character (an agent or a clone can write one): it must stay a
  // normal row -- icon, pointer, no state-row styling.
  paths: ['HEARTBEAT.md', 'notes/todo.md', 'src/app.py', 'src/util.py', 'trailing-marker\u200b'],
  directories: ['_bg', 'big', 'empty', 'notes', 'src', 'vault', 'vault/locked'],
  truncated: true,
  truncatedDirectories: ['big'],
  hiddenOnlyDirectories: ['_bg'],
  unreadableDirectories: ['vault/locked'],
}
// The server could not read the project root itself: the walk yielded nothing
// and `.` names the root in `unreadableDirectories`. A pre-fix dist sees only
// an empty listing here and says "No files in this workspace yet".
const TREE_ROOT_UNREADABLE = {
  root: PROJECT,
  repo: false,
  paths: [],
  directories: [],
  truncated: false,
  truncatedDirectories: [],
  hiddenOnlyDirectories: [],
  unreadableDirectories: ['.'],
}
// The project root's top level holds only folders the walk skips or hides
// (a `.kiro/`, a `node_modules/`): no file, no kept subdirectory, so the
// listing is as empty as an empty workspace's and `.` names the root in
// `hiddenOnlyDirectories`. A pre-fix dist says "No files in this workspace yet".
const TREE_ROOT_HIDDEN_ONLY = {
  root: PROJECT,
  repo: false,
  paths: [],
  directories: [],
  truncated: false,
  truncatedDirectories: [],
  hiddenOnlyDirectories: ['.'],
  unreadableDirectories: [],
}

const STATE_LABELS = {
  '_bg/': 'Contains only hidden items',
  'empty/': 'Empty folder',
  'big/': 'Files not shown: file limit of 10,000 reached',
}
// The truncation badge on the folder row. Post-fix it says "files not shown"
// and yields while the folder is expanded (its state row says the same thing);
// a pre-fix dist paints the old "files hidden" whether the folder is open or
// not -- the collision with the hidden-only row's vocabulary this PR removes.
const TRUNCATED_BADGE = EXPECT_STATE_ROWS ? 'files not shown' : 'files hidden'
// Every state row's name ends in this zero-width space -- the marker the
// tree's stylesheet selects (`src/pierre/treeStateRows.ts`), so a row that
// carries it is proven to be the styled synthetic row and not a real file.
const STATE_ROW_MARKER = '\u200b'
const isStateRow = (row, label) => row.text === label + STATE_ROW_MARKER

const slots = [{
  key: SLOT,
  title: 'Files tree',
  running: false,
  last_message: 'Files tree',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]
const t0 = Math.floor(Date.now() / 1000) - 900
const slotDetail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', content: 'Show me the workspace files.', ts: String(t0) },
    { role: 'assistant', content: 'The Files tab is open on the right.', ts: String(t0 + 30) },
  ],
}
const FILES_TAB = { id: 'files', kind: 'files', title: 'Files' }
const bucket = (tabs, activeId) => JSON.stringify({ activeId, tabs })

function pngSize(path) {
  const b = readFileSync(path)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

async function main() {
  console.log('dist:', DIST, ' prefix:', PREFIX, ' expect state rows:', EXPECT_STATE_ROWS)
  const { srv, base } = await serveDist(DIST)
  const executablePath = chromiumExecutable()
  const browser = await chromium.launch({ executablePath })
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

  let treeState = 'fixture'
  const extra = async (path, route) => {
    if (path === '/api/chat/slots') return json(route, slots), true
    if (/^\/api\/chat\/slots\/[^/]+/.test(path)) return json(route, slotDetail), true
    if (path === '/api/project/tree') {
      if (treeState === 'fails') return json(route, { error: 'Couldn’t list the workspace.', code: 'project_tree_unavailable' }, 503), true
      if (treeState === 'root-unreadable') return json(route, TREE_ROOT_UNREADABLE), true
      if (treeState === 'root-hidden-only') return json(route, TREE_ROOT_HIDDEN_ONLY), true
      return json(route, TREE), true
    }
    // Not a repository: the git probes say so and the status query never fires.
    if (path === '/api/project/git') return json(route, { path: PROJECT, repo: false }), true
    if (path === '/api/project/git/status') return json(route, { repo: false, files: [] }), true
    if (path === '/api/project/git/log') return json(route, { repo: false, commits: [] }), true
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] }), true
    return false
  }
  await stubDashboardApi(page, { slots, extra })
  logPageProblems(page)

  const wrote = []
  function record(file, evidence) {
    const { w, h } = pngSize(file)
    const bytes = readFileSync(file).length
    const mbpp = Math.round((bytes * 1000) / (w * h))
    const over = w > MAX_EDGE || h > MAX_EDGE
    const blank = mbpp < MIN_MBPP
    console.log(`wrote ${file}  ${w}x${h}  ${bytes}B  ${mbpp} milli-bytes/px${over ? '  OVER 2000px' : ''}${blank ? '  LIKELY BLANK' : ''}`)
    for (const e of evidence) console.log(`      asserted ${e}`)
    wrote.push({ file, w, h, mbpp, over, blank })
    if (blank || over) throw new Error(`frame ${file}: fails the frame gate (blank=${blank} over=${over})`)
  }

  async function load() {
    await page.addInitScript(([slot, project, tabsJson]) => {
      localStorage.clear()
      localStorage.setItem('mc-theme', 'dark')
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot-chat', slot)
      localStorage.setItem('mc-activity-open:' + slot, 'true')
      localStorage.setItem('mc-panel-tabs:' + slot, tabsJson)
      localStorage.setItem('mc-files-rail-open', '1')
      localStorage.setItem('mc-files-rail-w', '360')
      localStorage.setItem('mc-side-panel-width', '820')
      localStorage.setItem('mc-git-panel-opened:' + slot + ':' + project, '1')
      localStorage.setItem('mc-chat-config', JSON.stringify({ pinLastPrompt: false, streamMode: 'immediate' }))
    }, [SLOT, PROJECT, bucket([FILES_TAB], 'files')])
    await page.goto(base + '/?sid=' + encodeURIComponent(SLOT), { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
  }

  const panel = () => page.locator('div:has(> .side-panel-strip)').last()

  /** Rows of the tree's shadow root, as [{path, parent, expanded, text, badge}]. */
  const rows = () => page.evaluate(() => {
    const root = document.querySelector('file-tree-container')?.shadowRoot
    if (!root) return null
    return [...root.querySelectorAll('[data-type="item"]:not([data-file-tree-sticky-row])')].map(el => ({
      path: el.getAttribute('data-item-path'),
      parent: el.getAttribute('data-item-parent-path'),
      expanded: el.getAttribute('aria-expanded'),
      // The row's name: `aria-label` is the whole label, where the content
      // section paints it twice (visible + MiddleTruncate's measurement layer).
      text: el.getAttribute('aria-label') ?? '',
      // The host's row decoration (the truncation badge); the git lane and the
      // action affordance live in sections of their own.
      badge: el.querySelector('[data-item-section="decoration"]')?.textContent?.trim() ?? '',
      // The styling hook a PLANNED state row carries (`STATE_ROW_ICON` in
      // `src/pierre/treeStateRows.ts`): the stylesheet keys on this decoration,
      // never on the path's marker suffix, which a real file name can share.
      stateIcon: !!el.querySelector('[data-item-section="decoration"] [data-icon-name="kirocrew-tree-state-row"]'),
      // A lock glyph would be a marker derived from a failed read; none may exist.
      lockIcon: !!el.querySelector('[data-item-section="decoration"] [data-icon-name="file-tree-icon-lock"]'),
      // What the sheet actually did to the row: inert, file glyph hidden.
      inert: getComputedStyle(el).pointerEvents === 'none',
      glyphHidden: (() => { const g = el.querySelector('[data-item-section="icon"]'); return !!g && getComputedStyle(g).visibility === 'hidden' })(),
    }))
  })
  const waitRows = async (pred, what) => {
    const deadline = Date.now() + 20000
    for (;;) {
      const r = await rows()
      if (r && pred(r)) return r
      if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}; rows=${JSON.stringify(r)}`)
      await page.waitForTimeout(150)
    }
  }
  const dirRow = (path) => page.locator(`file-tree-container [data-type="item"][data-item-path="${path}"]:not([data-file-tree-sticky-row])`).first()
  const expand = async (path) => {
    await dirRow(path).click()
    await waitRows(r => r.some(x => x.path === path && x.expanded === 'true'), `${path} expanded`)
  }
  const collapse = async (path) => {
    await dirRow(path).click()
    await waitRows(r => r.some(x => x.path === path && x.expanded === 'false'), `${path} collapsed`)
  }
  const childrenOf = (r, path) => r.filter(x => x.parent === path)

  async function shot(name, evidence) {
    const file = `${OUT}/${PREFIX}-${name}.png`
    await panel().screenshot({ path: file })
    record(file, evidence)
  }

  // ── Frame 10: five folders expanded ───────────────────────────────────────
  await load()
  await panel().waitFor({ state: 'visible', timeout: 20000 })
  await waitRows(r => r.some(x => x.path === '_bg/'), 'the tree to paint _bg')
  // `vault/` holds only `locked/`, so Pierre's `flattenEmptyDirectories` paints
  // the pair as ONE row whose path is the terminal directory: there is no
  // `vault/` row to expand, and none to call empty.
  for (const p of ['_bg/', 'empty/', 'big/', 'vault/locked/', 'src/']) await expand(p)
  await page.waitForTimeout(600)
  let r = await rows()
  const evidence10 = []
  for (const [path, label] of Object.entries(STATE_LABELS)) {
    const kids = childrenOf(r, path)
    if (EXPECT_STATE_ROWS) {
      if (kids.length !== 1 || !isStateRow(kids[0], label)) {
        throw new Error(`frame 10: expected exactly one state row "${label}" under ${path}, got ${JSON.stringify(kids)}`)
      }
      if (!kids[0].stateIcon || !kids[0].inert || !kids[0].glyphHidden) {
        throw new Error(`frame 10: the state row under ${path} must carry the decoration hook and be styled by it (inert, glyph hidden), got ${JSON.stringify(kids[0])}`)
      }
      evidence10.push(`${path} expanded → one state row "${label}" (${kids[0].path}, name ends in the U+200B marker; carries the state-row decoration, pointer-events none, file glyph hidden)`)
    } else {
      if (kids.length !== 0) throw new Error(`frame 10 (pre-fix dist): expected NO rows under ${path}, got ${JSON.stringify(kids)}`)
      evidence10.push(`${path} aria-expanded=true → 0 rows beneath it`)
    }
  }
  // No real row -- file or folder -- carries the hook or its styling. "Real"
  // is decided by the PLAN (the fixture's four state rows), never by the name
  // suffix: `trailing-marker\u200b` is a real file whose name ends in the marker.
  const plannedPaths = new Set(Object.entries(STATE_LABELS).map(([dir, label]) => dir + label + STATE_ROW_MARKER))
  const realRows = r.filter(x => !plannedPaths.has(x.path))
  const misStyled = realRows.filter(x => x.stateIcon || x.inert || x.glyphHidden)
  if (misStyled.length !== 0) {
    throw new Error(`frame 10: a real row carries the state-row hook or styling: ${JSON.stringify(misStyled)}`)
  }
  const markerNamed = realRows.find(x => x.path === 'trailing-marker\u200b')
  if (!markerNamed) {
    throw new Error(`frame 10: the real file whose name ends in U+200B must render as a row, got ${JSON.stringify(r.map(x => x.path))}`)
  }
  evidence10.push(`${realRows.length} real rows (including the file "trailing-marker" + U+200B): none carries the state-row decoration, none is inert, every file glyph visible`)
  if (r.some(x => x.path === 'vault/')) {
    throw new Error(`frame 10: vault/ must be flattened into vault/locked/, got its own row: ${JSON.stringify(r.filter(x => x.path === 'vault/'))}`)
  }
  evidence10.push('vault/ has no row of its own: flattened into vault/locked/ (its only entry), so nothing can call it empty')
  const lockedKids = childrenOf(r, 'vault/locked/')
  const lockedRow = r.find(x => x.path === 'vault/locked/')
  if (lockedKids.length !== 0) {
    throw new Error(`frame 10: vault/locked/ must have NO row beneath it (a failed read is the notice's to report), got ${JSON.stringify(lockedKids)}`)
  }
  // ...and no marker derived from the failed read on the folder row either: an
  // error reaches the user only through the notice above the tree.
  if (!lockedRow || lockedRow.lockIcon || lockedRow.badge !== '' || lockedRow.stateIcon) {
    throw new Error(`frame 10: vault/locked/ must be an ordinary folder row (no glyph, no badge), got ${JSON.stringify(lockedRow)}`)
  }
  if (r.some(x => x.lockIcon)) throw new Error(`frame 10: no row may carry a lock glyph, got ${JSON.stringify(r.filter(x => x.lockIcon))}`)
  evidence10.push('vault/locked/ expanded → 0 rows beneath it, an ordinary folder row (no glyph, no badge); the alert above the tree is its only surface')
  const srcKids = childrenOf(r, 'src/')
  if (srcKids.length !== 2 || srcKids.some(k => !/\.py$/.test(k.text))) {
    throw new Error(`frame 10: src/ must show its two files and no state row, got ${JSON.stringify(srcKids)}`)
  }
  evidence10.push(`src/ expanded → ${srcKids.map(k => k.text).join(', ')} and no state row`)
  const bigOpen = r.find(x => x.path === 'big/')
  if (EXPECT_STATE_ROWS) {
    if (!bigOpen || bigOpen.badge !== '') {
      throw new Error(`frame 10: big/ is expanded over its own state row, so its badge must have yielded; got ${JSON.stringify(bigOpen)}`)
    }
    evidence10.push('big/ expanded → no badge on the folder row (its state row says it once)')
  } else {
    if (!bigOpen || bigOpen.badge !== TRUNCATED_BADGE) {
      throw new Error(`frame 10 (pre-fix dist): expected the "${TRUNCATED_BADGE}" badge on big/, got ${JSON.stringify(bigOpen)}`)
    }
    evidence10.push(`big/ expanded → "${TRUNCATED_BADGE}" badge and nothing beneath it (pre-fix dist)`)
  }
  // The folder the server could not read is also named ABOVE the tree, where a
  // notice can carry the agent hand-off (the row beneath it cannot).
  const unreadableNotice = await page.evaluate(() => {
    const panelEl = document.querySelector('div > .side-panel-strip')?.parentElement
    const el = panelEl?.querySelector('[data-testid="workspace-tree-unreadable-notice"]')
    return {
      present: !!el,
      alert: el?.getAttribute('role') === 'alert',
      text: (el?.textContent ?? '').replace(/\s+/g, ' ').trim(),
      handoff: [...(el?.querySelectorAll('button') ?? [])].some(b => b.textContent?.trim() === 'Ask the agent'),
    }
  })
  console.log('DIAG unreadable-notice', JSON.stringify(unreadableNotice))
  if (EXPECT_STATE_ROWS) {
    if (!unreadableNotice.present || !unreadableNotice.alert || !unreadableNotice.text.includes('Folders not readable: vault/locked') || !unreadableNotice.handoff) {
      throw new Error(`frame 10: expected the "Folders not readable: vault/locked" alert above the tree with "Ask the agent", got ${JSON.stringify(unreadableNotice)}`)
    }
    evidence10.push('"Folders not readable: vault/locked" alert above the tree with the "Ask the agent" hand-off')
  } else if (unreadableNotice.present) {
    throw new Error(`frame 10 (pre-fix dist): no not-readable notice expected, got ${JSON.stringify(unreadableNotice)}`)
  }
  await shot('10-folders-expanded', evidence10)

  // ── Frame 11: collapse _bg and big again -- their rows must go with them, and
  //    big's badge, which yielded to its row, is back on the closed folder ────
  await collapse('_bg/')
  await collapse('big/')
  await page.waitForTimeout(400)
  r = await rows()
  if (childrenOf(r, '_bg/').length !== 0) throw new Error(`frame 11: rows still under a collapsed _bg: ${JSON.stringify(childrenOf(r, '_bg/'))}`)
  if (childrenOf(r, 'big/').length !== 0) throw new Error(`frame 11: rows still under a collapsed big: ${JSON.stringify(childrenOf(r, 'big/'))}`)
  const stillEmpty = childrenOf(r, 'empty/')
  if (EXPECT_STATE_ROWS && (stillEmpty.length !== 1 || !isStateRow(stillEmpty[0], STATE_LABELS['empty/']))) {
    throw new Error(`frame 11: empty/ lost its state row: ${JSON.stringify(stillEmpty)}`)
  }
  const bigClosed = r.find(x => x.path === 'big/')
  if (!bigClosed || bigClosed.badge !== TRUNCATED_BADGE) {
    throw new Error(`frame 11: expected the "${TRUNCATED_BADGE}" badge on the collapsed big/, got ${JSON.stringify(bigClosed)}`)
  }
  await shot('11-bg-collapsed', [
    '_bg/ aria-expanded=false → 0 rows beneath it',
    `big/ aria-expanded=false → 0 rows beneath it, "${TRUNCATED_BADGE}" badge on the folder row`,
    EXPECT_STATE_ROWS ? `empty/ still shows "${STATE_LABELS['empty/']}"` : 'empty/ still shows nothing (pre-fix dist)',
  ])

  // ── Frame 20: the listing request fails ───────────────────────────────────
  // NOT changed by this PR -- captured to settle the "can the fetch fail
  // silently?" question in the issue: every host gates the tree on
  // `useTreeState` and renders its own notice + Refresh in the tree's place.
  treeState = 'fails'
  await load()
  await panel().waitFor({ state: 'visible', timeout: 20000 })
  // One retry with a 1 s backoff (api/queryClient.ts) before the error settles.
  await page.waitForTimeout(4500)
  const failed = await page.evaluate(() => {
    const panelEl = document.querySelector('div > .side-panel-strip')?.parentElement
    const text = (panelEl?.innerText ?? '').replace(/\s+/g, ' ')
    return {
      notice: text.includes("Couldn't load the file tree"),
      refresh: [...(panelEl?.querySelectorAll('button') ?? [])].some(b => b.textContent?.trim() === 'Refresh'),
      skeleton: !!panelEl?.querySelector('[role="status"][aria-label="Loading workspace…"]'),
      tree: !!document.querySelector('file-tree-container'),
      text: text.slice(0, 200),
    }
  })
  console.log('DIAG listing-failed', JSON.stringify(failed))
  if (!failed.notice || !failed.refresh || failed.skeleton || failed.tree) {
    throw new Error(`frame 20: expected the host's "Couldn't load the file tree" notice with Refresh, no skeleton and no tree, got ${JSON.stringify(failed)}`)
  }
  await shot('20-listing-failed', [`host notice "Couldn't load the file tree" with Refresh; no skeleton, no tree mounted (text: ${failed.text})`])

  // ── Frame 30: the project root itself could not be read ───────────────────
  // The listing answers 200 with nothing in it and `.` in `unreadableDirectories`.
  // Post-fix the tree's place holds a notice that NAMES the folder and carries
  // the same two actions as the host's failed-listing notice (frame 20):
  // Refresh and the agent hand-off. A pre-fix dist reads the empty listing as
  // an empty workspace and says so -- the claim this PR removes one level
  // down, made about the whole tree.
  treeState = 'root-unreadable'
  await load()
  await panel().waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(1500)
  const rootState = await page.evaluate((project) => {
    const panelEl = document.querySelector('div > .side-panel-strip')?.parentElement
    const text = (panelEl?.innerText ?? '').replace(/\s+/g, ' ')
    const buttons = [...(panelEl?.querySelectorAll('button') ?? [])].map(b => b.textContent?.trim())
    return {
      named: text.includes(`Couldn't read the folder ${project}`),
      bareLabel: text.includes('Folder not readable'),
      empty: text.includes('No files in this workspace yet'),
      marked: !!panelEl?.querySelector('[data-testid="workspace-tree-root-unreadable"]'),
      perFolderNotice: !!panelEl?.querySelector('[data-testid="workspace-tree-unreadable-notice"]'),
      alert: !!panelEl?.querySelector('[data-testid="workspace-tree-root-unreadable"] [role="alert"]'),
      refresh: buttons.includes('Refresh'),
      handoff: buttons.includes('Ask the agent'),
      skeleton: !!panelEl?.querySelector('[role="status"][aria-label="Loading workspace…"]'),
      tree: !!document.querySelector('file-tree-container'),
      text: text.slice(0, 240),
    }
  }, PROJECT)
  console.log('DIAG root-unreadable', JSON.stringify(rootState))
  if (rootState.skeleton || rootState.tree || rootState.perFolderNotice) {
    throw new Error(`frame 30: expected a settled state with no tree mounted, got ${JSON.stringify(rootState)}`)
  }
  if (EXPECT_STATE_ROWS) {
    if (!rootState.named || !rootState.marked || !rootState.alert || !rootState.refresh || !rootState.handoff || rootState.bareLabel || rootState.empty) {
      throw new Error(`frame 30: expected "Couldn't read the folder <root>" as an alert with Refresh and "Ask the agent", no bare label and no empty-workspace notice, got ${JSON.stringify(rootState)}`)
    }
    await shot('30-root-unreadable', [`"Couldn't read the folder ${PROJECT}" (data-testid workspace-tree-root-unreadable, role=alert) in the tree's place with a Refresh button and the "Ask the agent" hand-off; no "Folder not readable" bare label, no "No files in this workspace yet", no tree mounted (text: ${rootState.text})`])
  } else {
    if (!rootState.empty || rootState.named || rootState.bareLabel) {
      throw new Error(`frame 30 (pre-fix dist): expected the empty-workspace notice over the unreadable root, got ${JSON.stringify(rootState)}`)
    }
    await shot('30-root-unreadable', [`pre-fix dist: "No files in this workspace yet" painted over a root nothing read (text: ${rootState.text})`])
  }

  // ── Frame 31: the project root holds only skipped or hidden folders ───────
  // `.` in `hiddenOnlyDirectories`: the listing is as empty as an empty
  // workspace's, but the folder is not. Post-fix the tree's place holds the
  // hidden-only copy; there is nothing to act on, so no Refresh and no
  // hand-off. A pre-fix dist calls the workspace empty.
  treeState = 'root-hidden-only'
  await load()
  await panel().waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(1500)
  const hiddenRoot = await page.evaluate(() => {
    const panelEl = document.querySelector('div > .side-panel-strip')?.parentElement
    const text = (panelEl?.innerText ?? '').replace(/\s+/g, ' ')
    const buttons = [...(panelEl?.querySelectorAll('button') ?? [])].map(b => b.textContent?.trim())
    return {
      hiddenOnly: text.includes('This workspace contains only hidden items'),
      rowWording: text.includes('Contains only hidden items'),
      empty: text.includes('No files in this workspace yet'),
      marked: !!panelEl?.querySelector('[data-testid="workspace-tree-root-hidden-only"]'),
      perFolderNotice: !!panelEl?.querySelector('[data-testid="workspace-tree-unreadable-notice"]'),
      unreadable: !!panelEl?.querySelector('[data-testid="workspace-tree-root-unreadable"]'),
      refresh: buttons.includes('Refresh'),
      handoff: buttons.includes('Ask the agent'),
      skeleton: !!panelEl?.querySelector('[role="status"][aria-label="Loading workspace…"]'),
      tree: !!document.querySelector('file-tree-container'),
      text: text.slice(0, 200),
    }
  })
  console.log('DIAG root-hidden-only', JSON.stringify(hiddenRoot))
  if (hiddenRoot.skeleton || hiddenRoot.tree || hiddenRoot.unreadable || hiddenRoot.perFolderNotice) {
    throw new Error(`frame 31: expected a settled state with no tree and no not-readable notice, got ${JSON.stringify(hiddenRoot)}`)
  }
  if (EXPECT_STATE_ROWS) {
    if (!hiddenRoot.hiddenOnly || hiddenRoot.rowWording || !hiddenRoot.marked || hiddenRoot.empty || hiddenRoot.refresh || hiddenRoot.handoff) {
      throw new Error(`frame 31: expected "This workspace contains only hidden items" in the tree's place with no action row and no empty-workspace notice, got ${JSON.stringify(hiddenRoot)}`)
    }
    await shot('31-root-hidden-only', [`"This workspace contains only hidden items" (data-testid workspace-tree-root-hidden-only) in the tree's place; no Refresh, no hand-off, no "No files in this workspace yet", no tree mounted (text: ${hiddenRoot.text})`])
  } else {
    if (!hiddenRoot.empty || hiddenRoot.hiddenOnly) {
      throw new Error(`frame 31 (pre-fix dist): expected the empty-workspace notice over the hidden-only root, got ${JSON.stringify(hiddenRoot)}`)
    }
    await shot('31-root-hidden-only', [`pre-fix dist: "No files in this workspace yet" painted over a root whose top level holds only skipped or hidden folders (text: ${hiddenRoot.text})`])
  }

  console.log('\n── SUMMARY ─────────────────────────────')
  for (const w of wrote) console.log(` ok   ${w.w}x${w.h}  ${String(w.mbpp).padStart(4)} mB/px  ${w.file}`)
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
