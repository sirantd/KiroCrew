import { useState } from 'react'
import {
  Activity, Clock, Code2, Eye, FileText, ListChecks, RefreshCw, Search, Sparkles, type LucideIcon,
} from 'lucide-react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { KiroGhost } from './KiroGhost'
import { MemoryModeChip, type MemoryMode } from './MemoryModeChip'
import { useTheme } from '../hooks/useTheme'
import { getThemeBranding } from '../themeBranding'
import { api, type SuggestionItem, type SuggestionKind } from '../api/client'

import { i18nT } from '../i18n/t'
interface WelcomeViewProps {
  mode?: string
  setInput: (v: string) => void
  memoryMode?: string
  onSwitchMode?: (mode: MemoryMode) => void
}

/** Icon and tint per suggestion kind. */
export const SUGGESTION_KIND_STYLE: Record<SuggestionKind, { Icon: LucideIcon; tile: string }> = {
  code: { Icon: Code2, tile: 'bg-accent-subtle text-accent' },
  review: { Icon: Eye, tile: 'bg-info-subtle text-info' },
  ops: { Icon: Activity, tile: 'bg-danger-subtle text-danger' },
  tasks: { Icon: ListChecks, tile: 'bg-ok-subtle text-ok' },
  write: { Icon: FileText, tile: 'bg-warn-subtle text-warn' },
  research: { Icon: Search, tile: 'bg-info-subtle text-info' },
  schedule: { Icon: Clock, tile: 'bg-accent-subtle text-accent' },
  general: { Icon: Sparkles, tile: 'bg-accent-subtle text-accent' },
}

export interface Suggestion { text: string; kind: SuggestionKind }

/** Accept a legacy bare string or `{ text, kind }`; an unknown or missing kind becomes `general`. */
export function normalizeSuggestion(item: SuggestionItem): Suggestion {
  if (typeof item === 'string') return { text: item, kind: 'general' }
  const kind = item.kind && Object.hasOwn(SUGGESTION_KIND_STYLE, item.kind) ? item.kind as SuggestionKind : 'general'
  return { text: item.text, kind }
}

/** Greeting catalog keys the non-orchestrator heading picks from; the last one follows the local hour. */
export function welcomeGreetingKeys(hour: number = new Date().getHours()): string[] {
  const timeOfDay = hour < 12
    ? 'components.welcomeView.greeting_good_morning'
    : hour < 18 ? 'components.welcomeView.greeting_good_afternoon' : 'components.welcomeView.greeting_good_evening'
  return [
    'components.welcomeView.what_can_i_do_for_you',
    'components.welcomeView.greeting_what_are_we_building_today',
    'components.welcomeView.greeting_where_should_we_start',
    'components.welcomeView.greeting_whats_on_your_mind',
    'components.welcomeView.greeting_ready_when_you_are',
    'components.welcomeView.greeting_good_to_see_you',
    timeOfDay,
  ]
}

/** Every greeting the heading can show in the active language, for tests that must not pin one. */
export function welcomeGreetings(): string[] {
  const keys = [0, 12, 18].flatMap(h => welcomeGreetingKeys(h))
  return [...new Set(keys)].map(k => i18nT(k))
}

function SuggestedCards({ setInput }: { setInput: (v: string) => void }) {
  const qc = useQueryClient()
  const [refreshing, setRefreshing] = useState(false)
  const { data, isFetching } = useQuery({
    queryKey: ['suggestions'],
    queryFn: () => api.suggestions(),
    staleTime: 5 * 60_000,
    refetchInterval: 10 * 60_000,
    refetchOnWindowFocus: false,
  })

  // Built at render (not module scope) so each i18nT() reads the active language;
  // the App remounts on a language switch, re-evaluating these.
  const fallbackSuggestions: Suggestion[] = [
    { text: i18nT('components.welcomeView.suggestion_pipeline_status'), kind: 'ops' },
    { text: i18nT('components.welcomeView.suggestion_triage_tickets'), kind: 'tasks' },
    { text: i18nT('components.welcomeView.suggestion_search_code'), kind: 'research' },
    { text: i18nT('components.welcomeView.suggestion_summarize_chat'), kind: 'general' },
    { text: i18nT('components.welcomeView.suggestion_write_design_doc'), kind: 'write' },
    { text: i18nT('components.welcomeView.suggestion_review_cr'), kind: 'review' },
  ]
  const cards = data?.suggestions?.length ? data.suggestions.map(normalizeSuggestion) : fallbackSuggestions

  const handleRefresh = async () => {
    setRefreshing(true)
    try {
      const fresh = await api.suggestions(true)
      qc.setQueryData(['suggestions'], fresh)
    } catch {}
    setRefreshing(false)
  }

  const spinning = isFetching || refreshing

  return (
    <div className="w-full max-w-[620px] mx-auto">
      <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-3">
        {cards.map(({ text, kind }, i) => {
          const { Icon, tile } = SUGGESTION_KIND_STYLE[kind]
          return (
            // Fixed-height cell: 2 lines at 13px/1.35 (35px) + 10px padding each side + 2px border.
            // The card is absolute inside it, so on hover/focus it grows DOWN over the next row
            // instead of reflowing the grid.
            <div key={`${i}-${text}`} className="relative h-[108px] hover:z-10 focus-within:z-10">
              {/* type=button + onMouseDown preventDefault stop the pill from taking
                  keyboard focus on click. Without this the focused pill is re-activated
                  by a follow-up Enter (re-firing setInput) instead of submitting via the
                  textarea, so the prompt appears to clear instead of send. */}
              <button
                type="button"
                data-kind={kind}
                onMouseDown={e => e.preventDefault()}
                onClick={() => setInput(text)}
                className="group absolute top-0 inset-x-0 min-h-full flex flex-col items-start gap-2.5 p-3.5 rounded-xl border border-border bg-card text-card-fg text-[13px] font-medium text-left overflow-hidden cursor-pointer transition-all duration-[220ms] ease-in-out hover:border-accent hover:shadow-lg focus-visible:border-accent focus-visible:shadow-lg"
              >
                <span aria-hidden="true" className={`w-8 h-8 shrink-0 rounded-lg flex items-center justify-center ${tile}`}>
                  <Icon size={16} />
                </span>
                <span className="min-w-0 leading-[1.35] line-clamp-2 max-h-[2.7em] transition-[max-height] duration-[220ms] ease-in-out group-hover:line-clamp-none group-hover:max-h-[8em] group-focus-visible:line-clamp-none group-focus-visible:max-h-[8em]">{text}</span>
              </button>
            </div>
          )
        })}
      </div>
      {/* z-20 sits above a hovered card (z-10), which grows over whatever is below the last row. */}
      <div className="relative z-20 flex justify-end mt-3">
        <button
          type="button"
          onClick={handleRefresh}
          disabled={spinning}
          className="inline-flex items-center gap-1.5 rounded-lg px-2 py-1 text-[12px] text-muted hover:text-text hover:bg-bg-hover bg-transparent transition-colors cursor-pointer disabled:cursor-default"
          title={i18nT('components.welcomeView.refresh_suggestions')}
          aria-label={i18nT('components.welcomeView.refresh_suggestions')}
        >
          <span>{i18nT('components.welcomeView.refresh_suggestions')}</span>
          <RefreshCw size={12} className={spinning ? 'animate-spin' : ''} />
        </button>
      </div>
    </div>
  )
}

export default function WelcomeView({
  mode,
  setInput,
  memoryMode,
  onSwitchMode,
}: WelcomeViewProps) {
  // One greeting per mount: the KEY is fixed here so a re-render never reshuffles,
  // while i18nT below still re-resolves it on a language switch.
  const [greetingKey] = useState(() => {
    const keys = welcomeGreetingKeys()
    return keys[Math.floor(Math.random() * keys.length)] ?? keys[0]
  })

  const isOrchestrator = mode === 'orchestrator'

  // Per-theme brand mark: a registered theme (via the themeBranding seam) may
  // supply its own logo — render it here too, not just in the App shell, so the
  // welcome screen matches the active theme. Falls back to the stock KiroGhost
  // when the theme registers no logo (the standalone build always does).
  const { colorTheme } = useTheme()
  const brandLogo = getThemeBranding(colorTheme)?.logo
  const brandMark = brandLogo
    ? <img src={brandLogo} alt="" aria-hidden="true" className={`${isOrchestrator ? 'w-16 h-16' : 'w-12 h-12'} drop-shadow-lg shrink-0 animate-float rounded-md object-contain`} />
    : <KiroGhost size={isOrchestrator ? 64 : 48} className="drop-shadow-lg shrink-0 animate-float" />

  // Outside orchestrator mode the memory chip lives above the composer (ChatPage renders it), not here.
  if (!isOrchestrator) {
    return (
      // The unprefixed stack is the narrow/short-viewport baseline: start alignment keeps
      // overflowing cards reachable from the scroll origin. Wide, tall viewports switch to
      // the spread grid, preserving the greeting and card positions used on desktop.
      <div data-testid="welcome-layout" className="w-full flex-1 min-h-0 px-8 pt-12 flex flex-col justify-start gap-6 [@media(min-width:640px)_and_(min-height:600.01px)]:pt-0 [@media(min-width:640px)_and_(min-height:600.01px)]:grid [@media(min-width:640px)_and_(min-height:600.01px)]:grid-cols-1 [@media(min-width:640px)_and_(min-height:600.01px)]:grid-rows-[1.3fr_auto_0.7fr] [@media(min-width:640px)_and_(min-height:600.01px)]:justify-items-stretch [@media(min-width:640px)_and_(min-height:600.01px)]:gap-0">
        <div className="flex flex-col items-center w-full shrink-0 min-h-0">
          <div aria-hidden="true" className="hidden basis-[45%] shrink min-h-4 [@media(min-width:640px)_and_(min-height:600.01px)]:block" />
          <div className="flex flex-col items-center gap-3 text-center shrink-0">
            {brandMark}
            <h2 className="text-3xl sm:text-4xl font-light text-text-strong tracking-tight">{i18nT(greetingKey)}</h2>
          </div>
          <div aria-hidden="true" className="hidden grow min-h-8 [@media(min-width:640px)_and_(min-height:600.01px)]:block" />
        </div>
        <SuggestedCards setInput={setInput} />
        <div aria-hidden="true" className="hidden [@media(min-width:640px)_and_(min-height:600.01px)]:block" />
      </div>
    )
  }

  return (
    <div className="flex flex-col items-center w-full gap-6 px-8">
      {brandMark}
      <div className="text-center">
        <div className="flex items-center justify-center gap-4">
          <h2 className="text-3xl sm:text-5xl font-light text-text-strong tracking-tight">{i18nT('components.welcomeView.autopilot')}</h2>
        </div>
        <p className="text-[13px] text-muted mt-1">{i18nT('components.welcomeView.simple_tasks_run_instantly_complex_ones_get_a_pl')}</p>
      </div>
      <button
        className="px-4 py-2 rounded-lg text-[13px] text-muted border border-border bg-card hover:border-accent hover:text-text transition-all cursor-pointer"
        onClick={() => setInput('Create a plan to analyze Kiro Crew code package and report file count by major components')}
      >
        {i18nT('components.welcomeView.try_create_a_plan_to_analyze_kirocrew_code_packa')}
      </button>
      {onSwitchMode && <MemoryModeChip memoryMode={memoryMode} onSwitchMode={onSwitchMode} />}
    </div>
  )
}
