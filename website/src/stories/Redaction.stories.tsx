import type { Meta, StoryObj } from '@storybook/react-vite'
import { useEffect, useRef } from 'react'
import MarkdownRenderer from '../components/MarkdownRenderer'

/**
 * The redaction UI, reproducing the RFC prototype's conversation
 * (docs/request-for-change/assets/redaction-explain-reveal-prototype.html,
 * variants C then A): a credentials file with three removed values, a blocked
 * reviewers link, and the first-run coach. Stories that show a card open it by
 * clicking its marker, the same click a reader makes.
 */

const CRED = '[REDACTED: credential]'
const LINK = '[REDACTED: suspicious URL to reviews.corp.example]'
const REVIEWS_FILTER = encodeURIComponent(JSON.stringify({
  status: ['open'], owner: 'rayrayxu', range: ['3', '0', 'd'].join(''), cols: ['id', 'title', 'owner'],
}))
const REVIEWS_URL =
  `https://reviews.corp.example/reviews?filter=${REVIEWS_FILTER}&sort=-created&view=table`
  + '&page=1&per_page=50&since=2026-09-01&until=2026-09-30&labels=needs-review%2Csecurity&team=platform'

const REPLY = [
  '**Your dev profile is set up correctly.** Region is `us-west-2`; `~/.aws/credentials` has complete key pairs for both profiles, and dev also carries a session token (it expires):',
  '',
  '```ini',
  '[default]',
  'aws_access_key_id = AKIA…EXAMPLE',
  CRED,
  '',
  '[dev]',
  'aws_access_key_id = AKIA…DEVKEY',
  CRED,
  CRED,
  '```',
  '',
  `The reviewers filter page: ${LINK}`,
  '',
  'To switch to dev and verify identity:',
  '',
  '```bash',
  'AWS_PROFILE=dev aws sts get-caller-identity',
  '```',
].join('\n')

const FILE = (section: string) => ({ type: 'file', path: '~/.aws/credentials', section })
const REDACTIONS = [
  { ordinal: 0, rule: 'aws_secret_access_key', label: 'aws_secret_access_key = ', source: FILE('default'), view_command: 'aws configure get aws_secret_access_key --profile default', profile_command: null },
  { ordinal: 1, rule: 'aws_secret_access_key', label: 'aws_secret_access_key = ', source: FILE('dev'), view_command: 'aws configure get aws_secret_access_key --profile dev', profile_command: null },
  { ordinal: 2, rule: 'aws_session_token', label: 'aws_session_token = ', source: FILE('dev'), view_command: 'aws configure get aws_session_token --profile dev', profile_command: 'AWS_PROFILE=dev aws sts get-caller-identity' },
]
const BLOCKED = [
  { domain: 'reviews.corp.example', rule: 'exfil_query_length', path: '/reviews', query_chars: REVIEWS_URL.length - REVIEWS_URL.indexOf('?') - 1, url: REVIEWS_URL, url_withheld: null },
]

/** Renders the reply, then clicks `selector` (the n-th match) so a card is open. */
function Reply({ click, nth = 0, then, coach = false, blocked = BLOCKED }: {
  click?: string
  nth?: number
  then?: string
  coach?: boolean
  blocked?: unknown
}) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!click) return
    const el = ref.current?.querySelectorAll<HTMLElement>(`[data-testid="${click}"]`)[nth]
    el?.click()
    if (then) setTimeout(() => ref.current?.querySelector<HTMLElement>(then)?.click(), 50)
  }, [click, nth, then])
  return (
    <div ref={ref} className="max-w-[900px] p-4 text-[14px] leading-6 text-text">
      <MarkdownRenderer content={REPLY} redactions={REDACTIONS} blockedLinks={blocked} slotKey="story" redactionCoach={coach} />
    </div>
  )
}

const meta = {
  title: 'Chat/Redaction',
  component: Reply,
  parameters: { layout: 'fullscreen' },
} satisfies Meta<typeof Reply>
export default meta
type Story = StoryObj<typeof meta>

/** Variant C: the first reply in a session with removed values carries the coach. */
export const FirstRunCoach: Story = { args: { coach: true } }
/** Variant A: later replies carry only the lock tags and the chip. */
export const InlineMarkers: Story = { args: {} }
export const CredentialCard: Story = { args: { click: 'credential-tag', nth: 0 } }
export const SessionTokenCard: Story = { args: { click: 'credential-tag', nth: 2 } }
export const CredentialMoreWays: Story = { args: { click: 'credential-tag', nth: 0, then: '[data-testid="credential-more"] summary' } }
export const BlockedLinkCard: Story = { args: { click: 'blocked-link-inspect' } }
export const BlockedLinkFullUrl: Story = { args: { click: 'blocked-link-inspect', then: '[data-testid="blocked-link-review"] summary' } }
export const OpenOnceConfirm: Story = { args: { click: 'blocked-link-inspect', then: '[data-testid="blocked-link-open-once"]' } }
export const AllowConfirm: Story = { args: { click: 'blocked-link-inspect', then: '[data-testid="blocked-link-allow"]' } }
export const LinkAddressWithheld: Story = {
  args: {
    click: 'blocked-link-inspect',
    blocked: [{ ...BLOCKED[0], url: null, url_withheld: 'credential' }],
  },
}
