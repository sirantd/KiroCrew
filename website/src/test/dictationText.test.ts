import { describe, expect, it } from 'vitest'
import {
  dictationSeparator, dictationSpliceSpan, joinTranscript, locateDictationSpan, spliceDictationText, transcriptTail,
} from '../lib/dictationText'

describe('dictation text boundaries', () => {
  it('inserts Chinese at the cursor without artificial spaces on either side', () => {
    expect(spliceDictationText('请处理', '继续', { start: 1, end: 1 })).toEqual({ value: '请继续处理', caret: 3 })
  })

  it('replaces the selected region and restores the caret after Chinese speech', () => {
    expect(spliceDictationText('请删除处理', '继续', { start: 1, end: 3 })).toEqual({ value: '请继续处理', caret: 3 })
  })

  it('keeps English words separated without moving the caret past the existing suffix', () => {
    expect(spliceDictationText('Pleaseprocess', 'continue', { start: 6, end: 6 })).toEqual({ value: 'Please continue process', caret: 15 })
  })

  it('dictates immediately before an existing comma without moving or replacing the suffix', () => {
    expect(spliceDictationText('hello, world', 'there', { start: 5, end: 5 })).toEqual({
      value: 'hello there, world', caret: 11,
    })
  })

  it.each(['.', '!', '?', '; next', ': next', ')', ']', '}'])(
    'keeps existing closing punctuation %s attached to inserted speech', (suffix) => {
      expect(spliceDictationText(`hello${suffix}`, 'there', { start: 5, end: 5 })).toEqual({
        value: `hello there${suffix}`, caret: 11,
      })
    },
  )

  it('joins recognized punctuation to the preceding utterance without swallowing following words', () => {
    expect(joinTranscript(['hello', ', world', '!'])).toBe('hello, world!')
    expect(joinTranscript(['hello', 'there'])).toBe('hello there')
  })

  it('preserves existing spaces, newlines and tabs around the insertion', () => {
    expect(spliceDictationText('请\n\t处理', '继续', { start: 2, end: 2 })).toEqual({ value: '请\n继续\t处理', caret: 4 })
    expect(spliceDictationText('Please  process', 'continue', { start: 7, end: 7 }).value).toBe('Please continue process')
  })

  it('uses the same joining rule when the composer has never had a caret', () => {
    expect(spliceDictationText('请', '继续', null)).toEqual({ value: '请继续', caret: 3 })
    expect(spliceDictationText('Please', 'continue', null).value).toBe('Please continue')
  })

  it('does not delete a selected draft when a hypothesis is withdrawn', () => {
    expect(spliceDictationText('请处理', '', { start: 0, end: 3 })).toEqual({ value: '请处理', caret: 0 })
  })

  it('joins committed utterances and partials without deduplicating intentional repetition', () => {
    expect(joinTranscript(['继续', '继续', '，请处理。'])).toBe('继续继续，请处理。')
    expect(joinTranscript(['Please', 'continue', 'processing.'])).toBe('Please continue processing.')
    expect(dictationSeparator('𠀀', '𠀁')).toBe('')
  })

  it('keeps a mixed-script caption tail instead of discarding it before an English space', () => {
    expect(transcriptTail('旧内容新的字幕 hello world', 16)).toBe('新的字幕 hello world')
    expect(transcriptTail('an unfinishedword hello', 12)).toBe('hello')
    expect(transcriptTail('an unfinishedword你好', 10)).toBe('你好')
    expect(transcriptTail('unfinished, hello', 9)).toBe('hello')
    expect(transcriptTail('unfinished, hello', 7)).toBe('hello')
  })

  it('respects the UTF-16 caption budget without splitting supplementary Han characters', () => {
    expect(transcriptTail('𠀀𠀁𠀂', 5)).toBe('𠀁𠀂')
    expect(transcriptTail('a singleword', 4)).toBe('word')
    expect(transcriptTail('你好', 2)).toBe('你好')
    expect(transcriptTail('你好', 0)).toBe('')
  })
})

describe('a dictation splice described as a span', () => {
  it('spans exactly the run the write added, with its far-side separator apart', () => {
    const span = dictationSpliceSpan('Pleaseprocess', 'continue', { start: 6, end: 6 })
    const { value, caret } = spliceDictationText('Pleaseprocess', 'continue', { start: 6, end: 6 })
    expect(value).toBe('Please continue process')
    expect(span).toEqual({ start: 6, end: 15, text: ' continue', trail: ' ', replaced: '' })
    expect(value.slice(span!.start, span!.end)).toBe(span!.text)
    // `end` is the caret the splice reports, so a caller holding the value it
    // wrote can record the span without measuring it again.
    expect(span!.end).toBe(caret)
  })

  it('reports the selected words the write consumed, which removing the span would not give back', () => {
    const span = dictationSpliceSpan('Please review the plan', 'release', { start: 7, end: 13 })
    expect(spliceDictationText('Please review the plan', 'release', { start: 7, end: 13 }).value)
      .toBe('Please release the plan')
    expect(span!.replaced).toBe('review')
  })

  it('spans the appended run when the composer never had a caret', () => {
    expect(dictationSpliceSpan('请', '继续', null))
      .toEqual({ start: 1, end: 3, text: '继续', trail: '', replaced: '' })
  })

  it('describes no span for a withdrawn hypothesis, since nothing was written', () => {
    expect(dictationSpliceSpan('请处理', '', { start: 0, end: 3 })).toBeNull()
  })
})

describe('finding a written span in a value the user has edited', () => {
  const written = 'ask about the rollout and the dates'
  const span = { start: 21, end: 35, text: ' and the dates' }

  it('shifts the span by the edit when the typing happened before it', () => {
    expect(locateDictationSpan(written, `PLEASE ${written}`, span)).toBe(28)
  })

  it('leaves the span where it was when the typing happened after it', () => {
    expect(locateDictationSpan(written, `${written} today`, span)).toBe(21)
  })

  it('finds the span untouched in a value nobody edited', () => {
    expect(locateDictationSpan(written, written, span)).toBe(21)
  })

  it('refuses when the edit reached into the span, so those characters are not only the machine\'s', () => {
    expect(locateDictationSpan(written, 'ask about the rollout and the DAYS', span)).toBe(-1)
    expect(locateDictationSpan(written, 'ask about the rollout instead', span)).toBe(-1)
  })

  it('refuses rather than take an identical phrase the user typed themselves', () => {
    // The run is gone from where it was written and the same words sit elsewhere.
    expect(locateDictationSpan(written, 'ask and the dates about the rollout urgently', span)).toBe(-1)
  })

  it('refuses when the typing repeats the run, so both readings hold the same text', () => {
    // Typing `ship ` in front of a dictated `ship` and typing ` ship` after it
    // produce the same value, and the run's text then sits at BOTH offsets. One
    // of the two copies is the user's and nothing here says which.
    const ship = { start: 7, end: 11, text: 'ship' }
    expect(locateDictationSpan('please ship', 'please ship ship', ship)).toBe(-1)
    // One copy only, so the same shape still resolves.
    expect(locateDictationSpan('please ship', 'kindly please ship', ship)).toBe(14)
  })
})
