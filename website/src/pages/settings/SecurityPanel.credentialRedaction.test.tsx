/**
 * Credential redaction switch (Settings -> Security).
 *
 * The card is a VIEW over the switch the `/api/security/credential-redaction`
 * endpoints own, so what is pinned here is what a UI can get wrong in a way that
 * MISLEADS an owner about a security control, not the backend's rules (those are
 * in test/test_credential_redaction_switch.py):
 *
 *  - The recorded position is what renders, and flipping it sends exactly that
 *    boolean to the writer.
 *  - OFF is loud: a notice with the time it was switched off, so an owner who
 *    forgot cannot mistake the page for the default.
 *  - A FAILED READ renders NO switch. Showing the default (ON) for a state we
 *    could not read is the reassuring direction of wrong, and the dangerous one.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, cleanup } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import type { CredentialRedactionState } from '../../api/client'

vi.mock('../../api/client', () => ({
  api: {
    // The panel's rail reads these on mount regardless of the selected section,
    // and they must RESOLVE: a bare vi.fn() returns undefined, which react-query
    // rejects with "Query data cannot be undefined".
    deniedCommands: vi.fn(),
    governancePolicy: vi.fn(),
    securityPosture: vi.fn(),
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
    tailnetStatus: vi.fn(),
    credentialRedaction: vi.fn(),
    setCredentialRedaction: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { SecurityPanel } from './SecurityPanel'

const ON: CredentialRedactionState = { enabled: true, changed_at: '' }
const OFF: CredentialRedactionState = { enabled: false, changed_at: '2026-09-24T23:00:00+00:00' }

async function renderRedaction(state: CredentialRedactionState) {
  ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(state)
  const utils = renderWithProviders(<SecurityPanel />, { route: '/?section=redaction' })
  await screen.findByTestId('credential-redaction-row')
  return utils
}

function toggle(): HTMLElement {
  return screen.getByRole('switch')
}

describe('SecurityPanel - credential redaction switch', () => {
  beforeEach(() => {
    cleanup()
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue({
      builtins: [], user_added: [], disable_all: false, effective_count: 0, governance_locked: false,
    })
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue({ controls: [], counts: {} })
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue({
      version: null, has_policy: false, profile: null, unavailable: false, scopes: [],
    })
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    ;(api.patchConfig as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true })
    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockResolvedValue({
      enabled: false, governance_pinned: false, host: '', origin: '', resolved_at: 0, state: 'off',
    })
  })

  it('renders the recorded position, ON by default, with no off-notice', async () => {
    await renderRedaction(ON)
    expect(toggle()).toHaveAttribute('aria-checked', 'true')
    expect(screen.queryByTestId('credential-redaction-off-notice')).toBeNull()
  })

  it('switching off sends exactly {enabled: false} and shows the off-notice with the time', async () => {
    ;(api.setCredentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(OFF)
    await renderRedaction(ON)
    // The card re-reads on settlement, so the read must now report the new state,
    // as the real backend does.
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(OFF)
    fireEvent.click(toggle())
    await waitFor(() => expect(api.setCredentialRedaction).toHaveBeenCalledWith(false))
    await waitFor(() => expect(toggle()).toHaveAttribute('aria-checked', 'false'))
    const notice = await screen.findByTestId('credential-redaction-off-notice')
    expect(notice.textContent).toMatch(/off/i)
    expect(notice.textContent).toMatch(/2026/)
  })

  it('switching back on sends exactly {enabled: true} and clears the notice', async () => {
    ;(api.setCredentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(ON)
    await renderRedaction(OFF)
    expect(screen.getByTestId('credential-redaction-off-notice')).toBeTruthy()
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(ON)
    fireEvent.click(toggle())
    await waitFor(() => expect(api.setCredentialRedaction).toHaveBeenCalledWith(true))
    await waitFor(() => expect(screen.queryByTestId('credential-redaction-off-notice')).toBeNull())
  })

  it('a failed read renders no switch and says the read failed', async () => {
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=redaction' })
    await screen.findByTestId('credential-redaction-read-failed')
    expect(screen.queryByRole('switch')).toBeNull()
    expect(screen.queryByTestId('credential-redaction-row')).toBeNull()
  })

  it('a write whose response is lost still converges on the authoritative read', async () => {
    // The PUT landed server-side but the response never arrived: the card must
    // not keep showing ON while OFF is in force.
    ;(api.setCredentialRedaction as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('network'))
    await renderRedaction(ON)
    ;(api.credentialRedaction as ReturnType<typeof vi.fn>).mockResolvedValue(OFF)
    fireEvent.click(toggle())
    await waitFor(() => expect(toggle()).toHaveAttribute('aria-checked', 'false'))
    await screen.findByTestId('credential-redaction-off-notice')
  })

  it('a failed write keeps the position in force and says so', async () => {
    ;(api.setCredentialRedaction as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('403'))
    await renderRedaction(ON)
    fireEvent.click(toggle())
    await waitFor(() => expect(api.setCredentialRedaction).toHaveBeenCalledWith(false))
    await screen.findByTestId('credential-redaction-write-failed')
    expect(toggle()).toHaveAttribute('aria-checked', 'true')
    expect(screen.queryByTestId('credential-redaction-off-notice')).toBeNull()
  })
})
