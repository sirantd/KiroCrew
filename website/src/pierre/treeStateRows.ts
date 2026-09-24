/**
 * The one row the workspace tree puts under an expanded folder that has
 * nothing beneath it.
 *
 * The listing (`GET /api/project/tree`) arrives whole: there is no per-folder
 * request, so whether a folder has children is decided by the payload the
 * moment it lands, never by a fetch in flight. What the payload can leave
 * childless is a folder that is
 *
 *  - `empty`: listed as a directory with no file and no subfolder in it;
 *  - `hidden-only`: not empty on disk, but every entry in it is a folder the
 *    listing skips or hides (a dot-directory, a tooling cache, a symlink to a
 *    directory the walk does not follow) -- the server names these in
 *    `hiddenOnlyDirectories`, and without that signal `_bg/` holding only
 *    `.kiro/` would be called empty;
 *  - `truncated`: it has files, but the workspace file cap left it none --
 *    the server names these in `truncatedDirectories`.
 *
 * A folder the server could not read (`unreadableDirectories`) gets NO state
 * row and no marker of any kind. That value is the outcome of a failed
 * `scandir`, and an error reaches the user only through `ErrorNotice` with the
 * agent hand-off (the wrapper renders one above the tree naming the folders) --
 * a status line or a glyph inside the widget can carry neither. The folder is
 * still an ordinary row of its own (the server lists it in `directories`), and
 * it is skipped here so it is never called empty either.
 *
 * `@pierre/trees` renders rows for paths and has no slot for a status line, so
 * each state row is fed to the model as ONE synthetic child path whose
 * basename is the label followed by `STATE_ROW_MARKER`. That buys the widget's
 * own indentation, virtualization and keyboard reachability for free; the
 * wrapper keeps the row inert (no open, no selection, no context menu) and
 * `PIERRE_TREE_STATE_ROW_CSS` styles it as a status line. While the panel's
 * filter is active the wrapper withholds the rows from the model, so a status
 * line is never a search result.
 */

export type TreeStateRowKind = 'empty' | 'hidden-only' | 'truncated'

export type TreeStateRowLabels = Readonly<Record<TreeStateRowKind, string>>

export interface TreeStateRowPlan {
  /**
   * Synthetic child paths, one per childless folder, in folder order. A Set:
   * the path list spreads it into the model in that order, and the selection
   * and context-menu guards ask it whether a model path is a state row --
   * nothing reads a kind back, since the label the path ends in already says
   * which state it stands for.
   */
  paths: ReadonlySet<string>
  /**
   * The childless folders those rows sit under. The truncation badge on a
   * folder row yields while the state row beneath it is showing, so that a
   * folder whose files were cut does not say so twice.
   */
  folders: ReadonlySet<string>
}

/** The payload fields the plan reads; a subset of `api.projectTree`'s result. */
export interface TreeStateRowPayload {
  paths: readonly string[]
  directories?: readonly string[]
  truncatedDirectories?: readonly string[]
  hiddenOnlyDirectories?: readonly string[]
  unreadableDirectories?: readonly string[]
}

const parentOf = (path: string): string => {
  const cut = path.lastIndexOf('/')
  return cut === -1 ? '' : path.slice(0, cut)
}

/**
 * Zero-width space: paints nothing, screen readers skip it, and no real name
 * ends in one. Trailing, so the selector is one `data-item-path$=` suffix.
 */
export const STATE_ROW_MARKER = '\u200b'

/**
 * The decoration icon a planned state row carries -- the ONLY hook the
 * stylesheet (`PIERRE_TREE_STATE_ROW_CSS`) styles by. The path marker cannot be
 * that hook: a real file can end in U+200B too (an agent wrote it, a clone
 * brought it), and a sheet keyed on the path would paint it inert and iconless.
 * The wrapper's `renderRowDecoration` emits this icon only for a path in the
 * plan -- the same Set its selection and context-menu guards consult -- so
 * styling and behaviour share one source of truth. Pierre renders it as
 * `<svg data-icon-name="...">` with no symbol behind it (zero-sized here, and
 * hidden by the sheet); the marker stays the row's identity in the path.
 */
export const STATE_ROW_ICON = 'kirocrew-tree-state-row'

/** The decoration the wrapper returns for a planned state row. */
export const STATE_ROW_DECORATION = { icon: { name: STATE_ROW_ICON, width: 0, height: 0 } } as const

/**
 * A label becomes a path SEGMENT, so it must not contain the separator: a
 * translation written as "no files / hidden" would otherwise mint a subfolder.
 * The division slash (U+2215) reads the same and is an ordinary name character.
 *
 * The segment then ends in `STATE_ROW_MARKER`, and that marker -- not the label
 * -- is what `PIERRE_TREE_STATE_ROW_CSS` selects: a real file that happens to be
 * named like a label ("Empty folder", "Dossier vide") must keep its icon and
 * its pointer, and the stylesheet is fixed at model construction while the
 * labels follow the active language.
 */
const stateRowSegment = (label: string): string => label.replace(/\//g, '\u2215') + STATE_ROW_MARKER

export function planTreeStateRows(tree: TreeStateRowPayload, labels: TreeStateRowLabels): TreeStateRowPlan {
  // Every directory the payload knows: the explicit skeleton (which the server
  // sends even for folders whose files were cut) plus every ancestor of a
  // listed file or of an explicit directory.
  const directories = new Set<string>()
  const addWithAncestors = (dir: string) => {
    for (let d = dir; d; d = parentOf(d)) {
      if (directories.has(d)) break
      directories.add(d)
    }
  }
  for (const d of tree.directories ?? []) addWithAncestors(d.replace(/\/$/, ''))
  for (const p of tree.paths) addWithAncestors(parentOf(p))

  const withChild = new Set<string>()
  for (const p of tree.paths) withChild.add(parentOf(p))
  for (const d of directories) withChild.add(parentOf(d))

  const truncated = new Set(tree.truncatedDirectories ?? [])
  const unreadable = new Set(tree.unreadableDirectories ?? [])
  const hiddenOnly = new Set(tree.hiddenOnlyDirectories ?? [])
  const segments = {
    empty: stateRowSegment(labels.empty),
    'hidden-only': stateRowSegment(labels['hidden-only']),
    truncated: stateRowSegment(labels.truncated),
  } as const

  const paths = new Set<string>()
  const folders = new Set<string>()
  for (const dir of directories) {
    if (withChild.has(dir)) continue
    // Never read, so nothing beneath it is known: no row makes a claim, and
    // the failure itself is the notice's to report (see the header).
    if (unreadable.has(dir)) continue
    // Truncation is checked first: a folder that lost its files to the cap is
    // not empty, and the server never lists a folder with files as hidden-only;
    // the order only fixes what a malformed payload shows.
    const kind: TreeStateRowKind = truncated.has(dir)
      ? 'truncated'
      : hiddenOnly.has(dir)
        ? 'hidden-only'
        : 'empty'
    paths.add(`${dir}/${segments[kind]}`)
    folders.add(dir)
  }
  return { paths, folders }
}
