import { screen, fireEvent, cleanup } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import { MemoryModeChip } from './MemoryModeChip'

const rect = (top: number, left: number, w = 120, h = 24) =>
  ({ top, bottom: top + h, left, right: left + w, width: w, height: h, x: left, y: top, toJSON: () => ({}) }) as DOMRect

describe('MemoryModeChip popover placement', () => {
  beforeEach(() => {
    vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(500)
    vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(120)
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 800 })
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 700 })
  })
  afterEach(() => { vi.restoreAllMocks() })

  const open = (r: DOMRect) => {
    renderWithProviders(<MemoryModeChip onSwitchMode={vi.fn()} />)
    const btn = screen.getByText('Choose memory mode').closest('button')!
    vi.spyOn(btn, 'getBoundingClientRect').mockReturnValue(r)
    fireEvent.click(btn)
    return screen.getByTestId('memory-mode-popover')
  }

  it('always opens upward, bottom-anchored 6px above the chip, even with room below', () => {
    for (const top of [640, 100]) {
      const pop = open(rect(top, 340))
      expect(pop.style.bottom).toBe(`${700 - top + 6}px`)
      expect(pop.style.top).toBe('')
      expect(pop.style.visibility).toBe('')
      cleanup()
    }
  })

  it('clamps to the left viewport edge', () => {
    expect(open(rect(600, 0, 40)).style.left).toBe('8px')
  })

  it('clamps to the right viewport edge', () => {
    expect(open(rect(600, 780, 20)).style.left).toBe(`${800 - 8 - 500}px`)
  })

  it('closes on Escape and returns focus to the chip trigger', () => {
    renderWithProviders(<MemoryModeChip onSwitchMode={vi.fn()} />)
    const btn = screen.getByText('Choose memory mode').closest('button')!
    fireEvent.click(btn)
    const option = screen.getByText('Incognito').closest('button')!
    option.focus()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByTestId('memory-mode-popover')).not.toBeInTheDocument()
    expect(btn).toHaveFocus()
  })

  it('switching back from an ephemeral mode keeps the warn tint and rounded-lg shape', () => {
    const onSwitchMode = vi.fn()
    renderWithProviders(<MemoryModeChip memoryMode="temporary" onSwitchMode={onSwitchMode} />)
    const btn = screen.getByText('Temporary — switch to persistent mode').closest('button')!
    expect(btn.className).toContain('border-warn')
    expect(btn.className).toContain('rounded-lg')
    fireEvent.click(btn)
    expect(onSwitchMode).toHaveBeenCalledWith('persistent')
  })
})
