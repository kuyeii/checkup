import React, { forwardRef, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { renderAsync } from 'docx-preview'
import DiffMatchPatch from 'diff-match-patch'
import type { EditSummary } from '../types'
import { analyzeTableAppendPatch } from '../utils/aiRewritePatch'

function uid() {
  return Math.random().toString(16).slice(2) + Date.now().toString(16)
}

type BlockEl = HTMLElement & { dataset: { blockId?: string } }

type StructuralItem = {
  index: number
  kind: 'block' | 'table'
  element: HTMLElement
  text: string
  looseText: string
}

type EditVisual = {
  rects: Array<{ left: number; top: number; width: number; height: number }>
  marker?: { left: number; top: number; height: number }
  anchorX: number
  anchorY: number
}

type AppliedAiPatchRecord = {
  patchId: string
  blockId: string
  beforeText: string
  afterText: string
  blockHtmlBefore: string
  blockHtmlAfter: string
  originalTargetText: string
  targetText: string
  revisedText: string
  originalRevisedText: string
  startIndex: number
  endIndex: number
  keepUnderlinedDigits: boolean
}

type LocatedRiskHint = {
  riskKey: string
  blockId: string
  targetText: string
  anchorText: string
  evidenceText: string
  matchedText: string
  clauseUids: string[]
  updatedAt: number
}

const LOCK_PROGRESS_STEP_COUNT = 10

export type AppliedAiPatchSnapshot = {
  patchId: string
  targetText: string
  revisedText: string
}

export type DocumentEditorHandle = {
  locateRisk: (opts: { riskId?: string | number; riskSourceType?: string; targetText?: string; anchorText?: string; evidenceText?: string; clauseUids?: string[] }) => void
  scrollToBlock: (blockId: string) => void
  scrollToEdit: (editId: string) => void
  applyAiPatch: (opts: {
    patchId?: string | number
    targetText?: string
    revisedText?: string
    preserveRawTarget?: boolean
    anchorText?: string
    evidenceText?: string
    clauseUids?: string[]
    scroll?: boolean
  }) => boolean
  revertAiPatch: (patchId: string | number) => boolean
  getAppliedAiPatch: (patchId: string | number) => AppliedAiPatchSnapshot | null
  addSuggestionInsertComment: (opts: {
    riskId: string | number
    suggestionText: string
    riskSourceType?: string
    targetText?: string
    anchorText?: string
    evidenceText?: string
    clauseUids?: string[]
    scroll?: boolean
  }) => boolean
  removeSuggestionInsertComment: (riskId: string | number) => void
}

function plainTextOf(el: HTMLElement) {
  return (el.textContent || '').replace(/\u00a0/g, ' ')
}

function normalizeSearchText(text: string) {
  return text.replace(/\s+/g, '')
}

const LOOSE_IGNORABLE_RE = /[，。！？；：、“”‘’（）【】《》「」『』\[\]{}()<>.,!?;:'\"`~!@#$%^&*_\-+=|\\/]/g

function normalizeLooseSearchText(text: string) {
  return text.replace(/\s+/g, '').replace(LOOSE_IGNORABLE_RE, '').toLowerCase()
}

const CLAUSE_UID_PATTERN = /^segment_[A-Za-z0-9_-]+::[A-Za-z0-9_.()（）-]+$/

function stripLeadingClauseLabel(value: string) {
  return value.replace(/^(?:(?:第?\s*[0-9一二三四五六七八九十百千万零〇.]+(?:条|款))\s*)?(?:条款|条文|clause)?\s*/iu, '').trim()
}

function stripOuterWrappingQuotes(value: string) {
  let cleaned = String(value || '').trim()
  const quotePairs: Record<string, string> = {
    '“': '”',
    '「': '」',
    '"': '"',
    "'": "'",
  }
  while (cleaned.length >= 2) {
    const opening = cleaned[0]
    const closing = quotePairs[opening]
    if (!closing || cleaned[cleaned.length - 1] !== closing) break
    cleaned = cleaned.slice(1, -1).trim()
  }
  return cleaned
}

function sanitizeLocatorText(value: string) {
  const raw = String(value || '').trim()
  if (!raw) return ''
  const normalized = raw.replace(/\s+/g, ' ')

  let cleaned = normalized.replace(/^segment_[A-Za-z0-9_-]+::[A-Za-z0-9_.()（）-]+\s*/, '')
  cleaned = stripLeadingClauseLabel(cleaned)
  cleaned = cleaned.replace(/^(?:(?:条款|条文|clause)\s*)?(?:约定|规定|载明|提到|显示)?\s*[:：，,]?\s*/iu, '')
  cleaned = stripOuterWrappingQuotes(cleaned)
  cleaned = stripLeadingClauseLabel(cleaned)

  if (!cleaned) return ''
  if (CLAUSE_UID_PATTERN.test(cleaned)) return ''
  return cleaned
}

const TERMINAL_PUNCT_SET = new Set(['。', '！', '？', '；', '.', '!', '?', ';', ':', '：'])
const ENUMERATION_DELIM_SET = new Set(['、', '，', ','])

function adjustPatchBoundary(
  fullText: string,
  start: number,
  end: number,
  revisedText: string
) {
  let effectiveStart = start
  let effectiveEnd = end

  if (!fullText) {
    return { effectiveStart, effectiveEnd, effectiveTargetText: fullText.slice(start, end) }
  }

  if (!revisedText) {
    const leftChar = effectiveStart > 0 ? fullText[effectiveStart - 1] : ''
    const rightChar = effectiveEnd < fullText.length ? fullText[effectiveEnd] : ''
    if (rightChar && ENUMERATION_DELIM_SET.has(rightChar)) {
      effectiveEnd += 1
    } else if (leftChar && ENUMERATION_DELIM_SET.has(leftChar)) {
      effectiveStart -= 1
    }
  }

  if (revisedText && effectiveEnd < fullText.length) {
    const lastInsertedChar = revisedText[revisedText.length - 1]
    const boundaryChar = fullText[effectiveEnd]
    if (
      lastInsertedChar &&
      boundaryChar &&
      lastInsertedChar === boundaryChar &&
      TERMINAL_PUNCT_SET.has(lastInsertedChar)
    ) {
      effectiveEnd += 1
    }
  }

  return {
    effectiveStart,
    effectiveEnd,
    effectiveTargetText: fullText.slice(effectiveStart, effectiveEnd),
  }
}

function clamp(n: number, min: number, max: number) {
  return Math.max(min, Math.min(max, n))
}

function locateTextPosition(root: HTMLElement, targetIndex: number) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  let remaining = Math.max(0, targetIndex)
  let lastText: Text | null = null

  while (walker.nextNode()) {
    const node = walker.currentNode as Text
    const len = node.nodeValue?.length ?? 0
    lastText = node
    if (remaining <= len) {
      return { node, offset: remaining }
    }
    remaining -= len
  }

  if (lastText) {
    return { node: lastText, offset: lastText.nodeValue?.length ?? 0 }
  }
  return null
}

function buildRange(root: HTMLElement, start: number, end: number) {
  const a = locateTextPosition(root, start)
  const b = locateTextPosition(root, end)
  if (!a || !b) return null
  const range = document.createRange()
  range.setStart(a.node, clamp(a.offset, 0, a.node.nodeValue?.length ?? 0))
  range.setEnd(b.node, clamp(b.offset, 0, b.node.nodeValue?.length ?? 0))
  return range
}

function hasUnderlinedDigitsInRange(root: HTMLElement, start: number, end: number) {
  if (end <= start) return false
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  let cursor = 0
  while (walker.nextNode()) {
    const node = walker.currentNode as Text
    const text = node.nodeValue || ''
    if (!text) continue
    const nodeStart = cursor
    const nodeEnd = nodeStart + text.length
    cursor = nodeEnd
    if (nodeEnd <= start || nodeStart >= end) continue
    const segStart = Math.max(0, start - nodeStart)
    const segEnd = Math.min(text.length, end - nodeStart)
    const seg = text.slice(segStart, segEnd)
    if (!/\d/.test(seg)) continue
    let cur: HTMLElement | null = node.parentElement || root
    while (cur) {
      const deco = window.getComputedStyle(cur).textDecorationLine || ''
      if (deco.includes('underline')) return true
      if (cur === root) break
      cur = cur.parentElement
    }
  }
  return false
}

function shouldCloneInlineWrapper(sourceEl: Element | null) {
  if (!(sourceEl instanceof HTMLElement)) return false
  const tag = sourceEl.tagName.toLowerCase()
  if (["p", "div", "li", "ul", "ol", "table", "tbody", "thead", "tr", "td", "th"].includes(tag)) {
    return false
  }
  const display = window.getComputedStyle(sourceEl).display || ''
  if (["block", "list-item", "table", "table-row", "table-cell", "flex", "grid"].includes(display)) {
    return false
  }
  return true
}

function createReplacementFragment(
  sourceEl: Element | null,
  text: string,
  keepUnderlinedDigits: boolean,
  patchId?: string
) {
  const fragment = document.createDocumentFragment()
  const tokens = keepUnderlinedDigits ? text.match(/\d+|[^\d]+/g) || [] : [text]
  for (const token of tokens) {
    if (!token) continue
    const isDigit = keepUnderlinedDigits && /^\d+$/.test(token)
    if (shouldCloneInlineWrapper(sourceEl)) {
      const wrapper = (sourceEl as HTMLElement).cloneNode(false) as HTMLElement
      if (keepUnderlinedDigits) {
        wrapper.style.textDecoration = isDigit ? 'underline' : 'none'
      }
      if (patchId) wrapper.setAttribute('data-ai-patch-id', patchId)
      wrapper.textContent = token
      fragment.appendChild(wrapper)
    } else if (isDigit) {
      const span = document.createElement('span')
      span.style.textDecoration = 'underline'
      if (patchId) span.setAttribute('data-ai-patch-id', patchId)
      span.textContent = token
      fragment.appendChild(span)
    } else {
      if (patchId) {
        const span = document.createElement('span')
        span.setAttribute('data-ai-patch-id', patchId)
        span.textContent = token
        fragment.appendChild(span)
      } else {
        fragment.appendChild(document.createTextNode(token))
      }
    }
  }
  return fragment
}

function replaceTextRangePreserveStyle(
  block: HTMLElement,
  start: number,
  end: number,
  revisedText: string,
  keepUnderlinedDigits: boolean,
  patchId?: string
) {
  const range = buildRange(block, start, end)
  if (!range) return false
  const startParent =
    range.startContainer.nodeType === Node.TEXT_NODE
      ? (range.startContainer.parentElement as Element | null)
      : (range.startContainer as Element | null)
  const fragment = createReplacementFragment(startParent, revisedText, keepUnderlinedDigits, patchId)
  range.deleteContents()
  range.insertNode(fragment)
  block.normalize()
  return true
}

function findAllOccurrences(text: string, query: string) {
  const starts: number[] = []
  if (!query) return starts
  let from = 0
  while (from <= text.length - query.length) {
    const idx = text.indexOf(query, from)
    if (idx < 0) break
    starts.push(idx)
    from = idx + query.length
  }
  return starts
}

function buildCompactIndexMap(text: string) {
  let compact = ''
  const indexMap: number[] = []
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i]
    if (/\s/.test(ch)) continue
    compact += ch
    indexMap.push(i)
  }
  return { compact, indexMap }
}

function buildLooseIndexMap(text: string) {
  let loose = ''
  const indexMap: number[] = []
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i]
    LOOSE_IGNORABLE_RE.lastIndex = 0
    if (/\s/.test(ch) || LOOSE_IGNORABLE_RE.test(ch)) continue
    loose += ch.toLowerCase()
    indexMap.push(i)
  }
  return { loose, indexMap }
}

function findCompactOccurrencesWithRawRange(text: string, query: string) {
  const compactQuery = normalizeSearchText(query)
  if (!compactQuery) return [] as Array<{ start: number; end: number }>
  const mapped = buildCompactIndexMap(text)
  const ranges: Array<{ start: number; end: number }> = []
  let from = 0
  while (from <= mapped.compact.length - compactQuery.length) {
    const idx = mapped.compact.indexOf(compactQuery, from)
    if (idx < 0) break
    const startRaw = mapped.indexMap[idx]
    const endRaw = mapped.indexMap[idx + compactQuery.length - 1] + 1
    if (Number.isFinite(startRaw) && Number.isFinite(endRaw) && endRaw > startRaw) {
      ranges.push({ start: startRaw, end: endRaw })
    }
    from = idx + compactQuery.length
  }
  return ranges
}

function findLooseOccurrencesWithRawRange(text: string, query: string) {
  const looseQuery = normalizeLooseSearchText(query)
  if (looseQuery.length < 4) return [] as Array<{ start: number; end: number }>
  const mapped = buildLooseIndexMap(text)
  const ranges: Array<{ start: number; end: number }> = []
  let from = 0
  while (from <= mapped.loose.length - looseQuery.length) {
    const idx = mapped.loose.indexOf(looseQuery, from)
    if (idx < 0) break
    const startRaw = mapped.indexMap[idx]
    const endRaw = mapped.indexMap[idx + looseQuery.length - 1] + 1
    if (Number.isFinite(startRaw) && Number.isFinite(endRaw) && endRaw > startRaw) {
      ranges.push({ start: startRaw, end: endRaw })
    }
    from = idx + looseQuery.length
  }
  return ranges
}

function computeAppendOnlySuffix(sourceText: string, revisedText: string) {
  const source = String(sourceText || '')
  const revised = String(revisedText || '')
  if (!source || !revised || source === revised) return ''

  const matcher = new DiffMatchPatch()
  const diffs = matcher.diff_main(source, revised)
  matcher.diff_cleanupSemantic(diffs)

  let suffix = ''
  let consumedSource = 0
  let reachedSourceEnd = false
  for (const [op, chunk] of diffs) {
    if (!chunk) continue
    if (op === 0) {
      consumedSource += chunk.length
      if (reachedSourceEnd && normalizeSearchText(chunk)) return ''
      if (consumedSource >= source.length) {
        reachedSourceEnd = true
      }
      continue
    }
    if (op === -1) {
      return ''
    }
    if (op === 1) {
      if (!reachedSourceEnd && normalizeSearchText(chunk)) {
        return ''
      }
      suffix += chunk
    }
  }

  if (!reachedSourceEnd || !suffix.trim()) return ''
  return suffix
}

function extractAppendOnlySuffixFromClusterText(clusterText: string, revisedText: string) {
  const clusterLoose = normalizeLooseSearchText(String(clusterText || ''))
  const revised = String(revisedText || '')
  if (!clusterLoose || !revised) return ''

  const lines = revised.split(/\r?\n/)
  let lastSourceLine = -1
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i].trim()
    const loose = normalizeLooseSearchText(line)
    if (!loose) {
      if (/^[|:\-]+$/.test(line.replace(/\s+/g, ''))) {
        lastSourceLine = i
      }
      continue
    }
    if (clusterLoose.includes(loose)) {
      lastSourceLine = i
    }
  }
  if (lastSourceLine < 0 || lastSourceLine >= lines.length - 1) return ''
  const suffix = lines.slice(lastSourceLine + 1).join('\n').replace(/^\s+/, '').trim()
  return suffix
}

function extractAppendOnlySuffixFromSourceHint(sourceText: string, revisedText: string) {
  const source = String(sourceText || '')
  const revised = String(revisedText || '')
  if (!source || !revised || source === revised) return ''

  const sourceLines = source.split(/\r?\n/)
  const revisedLines = revised.split(/\r?\n/)
  const sourceSig: Array<{ rawIndex: number; loose: string }> = []
  const revisedSig: Array<{ rawIndex: number; loose: string }> = []

  for (let i = 0; i < sourceLines.length; i += 1) {
    const loose = normalizeLooseSearchText(sourceLines[i])
    if (loose) sourceSig.push({ rawIndex: i, loose })
  }
  for (let i = 0; i < revisedLines.length; i += 1) {
    const loose = normalizeLooseSearchText(revisedLines[i])
    if (loose) revisedSig.push({ rawIndex: i, loose })
  }

  if (sourceSig.length === 0 || revisedSig.length <= 1) return ''

  let bestMatchLen = 0
  let bestSuffixStart = -1
  for (let sourceStart = 0; sourceStart < sourceSig.length; sourceStart += 1) {
    let matchLen = 0
    while (
      sourceStart + matchLen < sourceSig.length &&
      matchLen < revisedSig.length &&
      sourceSig[sourceStart + matchLen].loose === revisedSig[matchLen].loose
    ) {
      matchLen += 1
    }
    if (matchLen === 0) continue
    if (sourceStart + matchLen !== sourceSig.length) continue
    if (matchLen >= revisedSig.length) continue
    let matchedChars = 0
    for (let offset = 0; offset < matchLen; offset += 1) {
      matchedChars += sourceSig[sourceStart + offset].loose.length
    }
    if (matchLen < 2 && matchedChars < 24) continue
    const suffixStart = revisedSig[matchLen].rawIndex
    if (
      matchLen > bestMatchLen ||
      (matchLen === bestMatchLen && (bestSuffixStart < 0 || suffixStart < bestSuffixStart))
    ) {
      bestMatchLen = matchLen
      bestSuffixStart = suffixStart
    }
  }

  if (bestSuffixStart < 0) return ''
  return revisedLines.slice(bestSuffixStart).join('\n').replace(/^\s+/, '').trim()
}

function collectSequentialBlockCluster(
  blocks: BlockEl[],
  startIndex: number,
  sourceText: string
) {
  const compactSource = normalizeLooseSearchText(String(sourceText || ''))
  if (!compactSource || startIndex < 0 || startIndex >= blocks.length) return [] as BlockEl[]

  const cluster: BlockEl[] = []
  let cursor = 0
  let matchedAny = false
  let skippedAfterMatch = 0

  for (let i = startIndex; i < blocks.length; i += 1) {
    const block = blocks[i]
    const blockText = plainTextOf(block)
    const compactBlock = normalizeLooseSearchText(blockText)
    if (!compactBlock) {
      if (!matchedAny) continue
      skippedAfterMatch += 1
      if (skippedAfterMatch >= 3) break
      continue
    }

    const idx = compactSource.indexOf(compactBlock, cursor)
    if (idx < 0) {
      if (!matchedAny) continue
      skippedAfterMatch += 1
      if (skippedAfterMatch >= 2) break
      continue
    }

    matchedAny = true
    skippedAfterMatch = 0
    cluster.push(block)
    cursor = idx + compactBlock.length
    if (cursor >= compactSource.length) break
  }

  if (cluster.length === 0) return [] as BlockEl[]
  const joinedCompact = normalizeLooseSearchText(cluster.map((block) => plainTextOf(block)).join('\n'))
  if (!joinedCompact || !compactSource.includes(joinedCompact)) return [] as BlockEl[]
  return cluster
}

function buildTableStructuralText(table: HTMLTableElement) {
  const lines = Array.from(table.rows)
    .map((row) => {
      const cells = Array.from(row.cells)
        .map((cell) => plainTextOf(cell as HTMLElement).replace(/\s+/g, ' ').trim())
        .filter((cell) => cell.length > 0)
      if (cells.length === 0) return ''
      return `| ${cells.join(' | ')} |`
    })
    .filter(Boolean)
  return lines.join('\n').trim()
}

function collectStructuralItems(root: HTMLElement) {
  const nodes = Array.from(root.querySelectorAll<HTMLElement>('p, li, table'))
  const items: StructuralItem[] = []

  nodes.forEach((node) => {
    const tag = node.tagName.toLowerCase()
    if (tag === 'table') {
      const text = buildTableStructuralText(node as HTMLTableElement)
      const looseText = normalizeLooseSearchText(text)
      if (!looseText) return
      items.push({ index: items.length, kind: 'table', element: node, text, looseText })
      return
    }

    if (node.closest('table')) return
    const text = plainTextOf(node).replace(/\s+/g, ' ').trim()
    const looseText = normalizeLooseSearchText(text)
    if (!looseText) return
    items.push({ index: items.length, kind: 'block', element: node, text, looseText })
  })

  return items
}

function collectSequentialStructuralCluster(
  items: StructuralItem[],
  startIndex: number,
  sourceText: string
) {
  const compactSource = normalizeLooseSearchText(String(sourceText || ''))
  if (!compactSource || startIndex < 0 || startIndex >= items.length) return [] as StructuralItem[]

  const cluster: StructuralItem[] = []
  let cursor = 0
  let matchedAny = false
  let skippedAfterMatch = 0

  for (let i = startIndex; i < items.length; i += 1) {
    const item = items[i]
    if (!item.looseText) continue

    const idx = compactSource.indexOf(item.looseText, cursor)
    if (idx < 0) {
      if (!matchedAny) continue
      skippedAfterMatch += 1
      if (skippedAfterMatch >= 2) break
      continue
    }

    matchedAny = true
    skippedAfterMatch = 0
    cluster.push(item)
    cursor = idx + item.looseText.length
    if (cursor >= compactSource.length) break
  }

  if (cluster.length === 0) return [] as StructuralItem[]
  const joinedCompact = cluster.map((item) => item.looseText).join('')
  if (!joinedCompact || !compactSource.includes(joinedCompact)) return [] as StructuralItem[]
  return cluster
}

type StructuredInsertionPlan = {
  cluster: StructuralItem[]
  anchorItem: StructuralItem
  insertPosition: 'before' | 'after'
  insertText: string
  matchedCharCount: number
  score: number
}

function tableCellTokens(tableText: string) {
  return String(tableText || '')
    .split('|')
    .map((part) => part.replace(/\s+/g, ' ').trim())
    .filter(Boolean)
    .filter((part) => !/^:?-{3,}:?$/.test(part))
}

function tableTokenOverlapRatio(left: string, right: string) {
  const leftTokens = Array.from(new Set(tableCellTokens(left)))
  if (leftTokens.length === 0) return 0
  const rightLoose = normalizeLooseSearchText(right)
  const hits = leftTokens.filter((token) => normalizeLooseSearchText(token) && rightLoose.includes(normalizeLooseSearchText(token))).length
  return hits / leftTokens.length
}

function previousStructuralItem(items: StructuralItem[], startIndex: number, kind?: StructuralItem['kind']) {
  for (let idx = startIndex - 1; idx >= 0; idx -= 1) {
    const item = items[idx]
    if (!item) continue
    if (kind && item.kind !== kind) continue
    return item
  }
  return null
}

function buildStructuredInsertionPlan(
  items: StructuralItem[],
  startIndex: number,
  sourceText: string,
  revisedText: string
): StructuredInsertionPlan | null {
  const cluster = collectSequentialStructuralCluster(items, startIndex, sourceText)
  if (cluster.length === 0) return null

  const revised = String(revisedText || '')
  const revisedLooseMap = buildLooseIndexMap(revised)
  if (!revisedLooseMap.loose) return null

  let cursor = 0
  let previousRawEnd = 0
  let matchedCharCount = 0
  const insertions: Array<{ insertText: string; anchorItem: StructuralItem; insertPosition: 'before' | 'after' }> = []

  for (let idx = 0; idx < cluster.length; idx += 1) {
    const item = cluster[idx]
    const matchAt = revisedLooseMap.loose.indexOf(item.looseText, cursor)
    if (matchAt < 0) return null

    const rawStart = revisedLooseMap.indexMap[matchAt]
    const rawEnd = revisedLooseMap.indexMap[matchAt + item.looseText.length - 1] + 1
    if (!Number.isFinite(rawStart) || !Number.isFinite(rawEnd) || rawEnd <= rawStart) return null

    const gapText = revised.slice(previousRawEnd, rawStart).trim()
    if (gapText) {
      insertions.push({
        insertText: gapText,
        anchorItem: idx === 0 ? item : cluster[idx - 1],
        insertPosition: idx === 0 ? 'before' : 'after',
      })
    }

    cursor = matchAt + item.looseText.length
    previousRawEnd = rawEnd
    matchedCharCount += item.looseText.length
  }

  const trailingText = revised.slice(previousRawEnd).trim()
  if (trailingText) {
    insertions.push({
      insertText: trailingText,
      anchorItem: cluster[cluster.length - 1],
      insertPosition: 'after',
    })
  }

  if (insertions.length !== 1) return null
  const insertion = insertions[0]
  const insertionLoose = normalizeLooseSearchText(insertion.insertText)
  if (insertionLoose) {
    const clusterIndexes = new Set(cluster.map((item) => item.index))
    const nearbyItems = items.filter((item) => {
      if (clusterIndexes.has(item.index)) return false
      if (item.looseText.length < 12) return false
      if (insertion.insertPosition === 'before') {
        return item.index >= insertion.anchorItem.index && item.index <= insertion.anchorItem.index + 1
      }
      return item.index > insertion.anchorItem.index && item.index <= insertion.anchorItem.index + 2
    })
    if (nearbyItems.some((item) => insertionLoose.includes(item.looseText))) {
      return null
    }
  }

  const score = cluster.length * 1000 + matchedCharCount * 2 - insertion.insertText.length

  return {
    cluster,
    anchorItem: insertion.anchorItem,
    insertPosition: insertion.insertPosition,
    insertText: insertion.insertText,
    matchedCharCount,
    score,
  }
}

function minimizePatchPair(targetText: string, revisedText: string) {
  const before = String(targetText || '')
  const after = String(revisedText || '')
  if (!before || !after || before === after) {
    return { targetText: before, revisedText: after }
  }

  let prefix = 0
  const limit = Math.min(before.length, after.length)
  while (prefix < limit && before[prefix] === after[prefix]) prefix += 1

  let suffix = 0
  while (
    suffix < before.length - prefix &&
    suffix < after.length - prefix &&
    before[before.length - 1 - suffix] === after[after.length - 1 - suffix]
  ) {
    suffix += 1
  }

  const minimizedTarget = before.slice(prefix, before.length - suffix)
  const minimizedRevised = after.slice(prefix, after.length - suffix)
  return {
    targetText: minimizedTarget || before,
    revisedText: minimizedRevised || after
  }
}
function findPatchMarkedNodes(block: HTMLElement, patchId: string) {
  const out: HTMLElement[] = []
  if (!patchId) return out
  const walker = document.createTreeWalker(block, NodeFilter.SHOW_ELEMENT)
  while (walker.nextNode()) {
    const node = walker.currentNode as HTMLElement
    if (node.getAttribute('data-ai-patch-id') === patchId) {
      out.push(node)
    }
  }
  return out
}

export const DocumentEditor = forwardRef<
  DocumentEditorHandle,
  {
    file: File | null
    edits: EditSummary[]
    onEditsChange: (edits: EditSummary[]) => void
    onReadyChange?: (ready: boolean) => void
    riskHighlights?: string[]
    clauseTextByUid?: Record<string, string>
    className?: string
    isInteractionLocked?: boolean
    lockLabel?: string
    lockProgress?: number | null
  }
>(function DocumentEditor(props, ref) {
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const rowRef = useRef<HTMLDivElement | null>(null)
  const docRef = useRef<HTMLDivElement | null>(null)
  const canvasRef = useRef<HTMLDivElement | null>(null)
  const [ready, setReady] = useState(false)
  const [balloonTops, setBalloonTops] = useState<Record<string, number>>({})
  const [gutterLeft, setGutterLeft] = useState<number>(0)
  const [paperEdgeX, setPaperEdgeX] = useState<number>(0)
  const [overlayHeight, setOverlayHeight] = useState<number>(0)
  const [visuals, setVisuals] = useState<Record<string, EditVisual>>({})
  const [linePaths, setLinePaths] = useState<Record<string, string>>({})
  const [trunkPaths, setTrunkPaths] = useState<string[]>([])
  const [manualComments, setManualComments] = useState<EditSummary[]>([])
  const allEdits = useMemo(() => [...props.edits, ...manualComments], [props.edits, manualComments])
  const hasComments = allEdits.length > 0

  const dmp = useMemo(() => new DiffMatchPatch(), [])

  const baselineRef = useRef<Map<string, string>>(new Map())
  const blockElsRef = useRef<Map<string, BlockEl>>(new Map())
  const editMapRef = useRef<Map<string, EditSummary>>(new Map())
  const debounceTimer = useRef<number | null>(null)
  const focusTimer = useRef<number | null>(null)
  const cardElsRef = useRef<Map<string, HTMLButtonElement>>(new Map())
  const sourceElsRef = useRef<Map<string, HTMLElement>>(new Map())
  const appliedAiPatchMapRef = useRef<Map<string, AppliedAiPatchRecord>>(new Map())
  const locatedRiskHintMapRef = useRef<Map<string, LocatedRiskHint>>(new Map())

  useEffect(() => {
    props.onReadyChange?.(ready)
  }, [ready, props.onReadyChange])

  const applyRiskHighlights = () => {
    const highlights = (props.riskHighlights || []).map((t) => normalizeSearchText(t)).filter(Boolean)
    for (const el of blockElsRef.current.values()) {
      const txt = normalizeSearchText(plainTextOf(el))
      const hit = highlights.some((h) => h.length >= 4 && txt.includes(h))
      el.classList.toggle('riskBlock', hit)
    }
  }

  const collectBlocks = () => {
    if (!docRef.current) return
    const blocks = Array.from(docRef.current.querySelectorAll<HTMLElement>('p, li, td, th'))
    const map = new Map<string, BlockEl>()

    blocks.forEach((el, idx) => {
      const b = el as BlockEl
      const id = b.dataset.blockId || `b_${idx + 1}`
      b.dataset.blockId = id
      b.setAttribute('contenteditable', props.isInteractionLocked ? 'false' : 'true')
      b.setAttribute('spellcheck', 'false')
      b.classList.add('editableBlock')
      b.classList.toggle('editableBlock--locked', Boolean(props.isInteractionLocked))
      map.set(id, b)
    })

    blockElsRef.current = map
    if (baselineRef.current.size === 0) {
      const base = new Map<string, string>()
      map.forEach((el, id) => {
        base.set(id, plainTextOf(el))
      })
      baselineRef.current = base
    }
  }

  const upsertInsertedParagraph = (opts: {
    anchorElement: HTMLElement
    position: 'before' | 'after'
    text: string
    patchId?: string
  }) => {
    const { anchorElement, position, text, patchId } = opts
    let insertionBlock: BlockEl | null = null
    let createdBlock = false

    const sibling =
      position === 'before'
        ? (anchorElement.previousElementSibling as HTMLElement | null)
        : (anchorElement.nextElementSibling as HTMLElement | null)

    if (sibling) {
      const siblingTag = sibling.tagName.toLowerCase()
      const siblingText = plainTextOf(sibling).trim()
      if ((siblingTag === 'p' || siblingTag === 'li') && !siblingText && sibling.closest('table') == null) {
        insertionBlock = sibling as BlockEl
      }
    }

    if (!insertionBlock) {
      insertionBlock = document.createElement('p') as BlockEl
      insertionBlock.className = 'editableBlock'
      insertionBlock.setAttribute('contenteditable', props.isInteractionLocked ? 'false' : 'true')
      insertionBlock.setAttribute('spellcheck', 'false')
      insertionBlock.classList.toggle('editableBlock--locked', Boolean(props.isInteractionLocked))
      const nextId = `b_${blockElsRef.current.size + 1}`
      insertionBlock.dataset.blockId = nextId
      if (anchorElement.parentNode) {
        if (position === 'before') {
          anchorElement.parentNode.insertBefore(insertionBlock, anchorElement)
        } else {
          anchorElement.parentNode.insertBefore(insertionBlock, anchorElement.nextSibling)
        }
      } else {
        return null
      }
      createdBlock = true
      collectBlocks()
    }

    const beforeText = plainTextOf(insertionBlock)
    const beforeHtml = insertionBlock.innerHTML
    insertionBlock.innerHTML = ''
    insertionBlock.appendChild(createReplacementFragment(insertionBlock, text, false, patchId || undefined))
    insertionBlock.normalize()
    const afterText = plainTextOf(insertionBlock)
    if (!afterText || afterText === beforeText) {
      if (createdBlock && insertionBlock.parentNode) {
        insertionBlock.parentNode.removeChild(insertionBlock)
        collectBlocks()
      }
      return null
    }

    return { insertionBlock, beforeText, beforeHtml, afterText }
  }

  const computeEdits = () => {
    const base = baselineRef.current
    const blocks = blockElsRef.current
    if (base.size === 0 || blocks.size === 0) return

    const nextEdits: EditSummary[] = []
    const nextMap = new Map<string, EditSummary>()

    blocks.forEach((el, blockId) => {
      const baseline = base.get(blockId) ?? ''
      const current = plainTextOf(el)
      const isChanged = baseline !== current
      el.classList.toggle('changedBlock', isChanged)
      if (!isChanged) return

      const diffs = dmp.diff_main(baseline, current)
      dmp.diff_cleanupSemantic(diffs)

      const grouped = new Map<number, { startIndex: number; insertedText: string; deletedText: string }>()
      let currentIndex = 0

      const ensureGroup = (startIndex: number) => {
        let g = grouped.get(startIndex)
        if (!g) {
          g = { startIndex, insertedText: '', deletedText: '' }
          grouped.set(startIndex, g)
        }
        return g
      }

      for (const [op, text] of diffs) {
        if (!text) continue
        if (op === 0) {
          currentIndex += text.length
          continue
        }
        const group = ensureGroup(currentIndex)
        if (op === 1) {
          group.insertedText += text
          currentIndex += text.length
        } else if (op === -1) {
          group.deletedText += text
        }
      }

      for (const group of Array.from(grouped.values()).sort((a, b) => a.startIndex - b.startIndex)) {
        // NOTE: Do NOT trim away whitespace-only edits.
        // Users still expect a visible "批注" balloon even if they only adjust spacing/line breaks.
        const insertedRaw = group.insertedText
        const deletedRaw = group.deletedText
        if (!insertedRaw && !deletedRaw) continue

        const normalizeForDisplay = (value: string) => value.replace(/\s+/g, ' ').trim()
        let insertedText = normalizeForDisplay(insertedRaw)
        let deletedText = normalizeForDisplay(deletedRaw)
        if (!insertedText && insertedRaw) insertedText = '(空白)'
        if (!deletedText && deletedRaw) deletedText = '(空白)'

        const type: EditSummary['type'] = insertedRaw && deletedRaw ? 'replace' : insertedRaw ? 'insert' : 'delete'
        const key = `${blockId}::${group.startIndex}::${insertedRaw.slice(0, 200)}::${deletedRaw.slice(0, 200)}`
        const prev = editMapRef.current.get(key)

        const summary: EditSummary = {
          id: prev?.id || uid(),
          blockId,
          type,
          insertedText: insertedText.slice(0, 160),
          deletedText: deletedText.slice(0, 160),
          updatedAt: prev?.updatedAt || Date.now(),
          startIndex: group.startIndex,
          endIndex: group.startIndex + insertedRaw.length
        }

        nextEdits.push(summary)
        nextMap.set(key, summary)
      }
    })

    editMapRef.current = nextMap
    props.onEditsChange(nextEdits)
    applyRiskHighlights()
  }

  const measureVisuals = (edits: EditSummary[]) => {
    const row = rowRef.current
    const canvas = canvasRef.current
    if (!row || !canvas) {
      setVisuals({})
      setBalloonTops({})
      setLinePaths({})
      return
    }

    const rowRect = row.getBoundingClientRect()
    const canvasRect = canvas.getBoundingClientRect()
    const next: Record<string, EditVisual> = {}

    // Compute a stable gutter X aligned with the document paper right edge.
    // IMPORTANT: The balloon cards should sit *next to* the paper edge (Figma-style),
    // not float far to the right.
    const wrapper = docRef.current?.querySelector<HTMLElement>('.docx-wrapper')
    const paperRect = (wrapper || docRef.current || canvas).getBoundingClientRect()
    const paperRightX = paperRect.right - rowRect.left
    // Align the LEFT edge of the comment cards to the paper's right edge.
    // (User expectation: the card "sticks" to the vertical paper boundary, with a dashed guide line.)
    // Do NOT clamp this left position based on the row width; instead we reserve space via
    // `.docRow--withComments { padding-right: var(--commentPadRight) }` so the gutter never overlaps.
    const nextGutterLeft = Math.max(0, paperRightX)
    setPaperEdgeX(paperRightX)
    setGutterLeft(nextGutterLeft)

    // Keep overlay layers tall enough to cover the whole rendered document.
    // Avoid 100vh sizing which breaks when document is longer than viewport.
    const h = Math.max(canvas.scrollHeight || 0, docRef.current?.scrollHeight || 0, row.scrollHeight || 0, canvasRect.height)
    setOverlayHeight(h)

    for (const edit of edits) {
      const block = blockElsRef.current.get(edit.blockId)
      if (!block) continue

      const rects: Array<{ left: number; top: number; width: number; height: number }> = []
      let anchorX = 0
      let anchorY = 0
      let marker: EditVisual['marker']

      if (edit.insertedText && edit.endIndex > edit.startIndex) {
        const range = buildRange(block, edit.startIndex, edit.endIndex)
        if (range) {
          const clientRects = Array.from(range.getClientRects())
          if (clientRects.length > 0) {
            for (const rect of clientRects) {
              rects.push({
                left: rect.left - canvasRect.left,
                top: rect.top - canvasRect.top,
                width: rect.width,
                height: rect.height
              })
            }
            const last = clientRects[clientRects.length - 1]
            anchorX = last.right - rowRect.left
            anchorY = last.top - rowRect.top + last.height / 2
          }
        }
      }

      if ((!edit.insertedText || rects.length === 0) && edit.deletedText) {
        const caret = buildRange(block, edit.startIndex, edit.startIndex)
        const caretRect = caret?.getBoundingClientRect()
        if (caretRect && (caretRect.width > 0 || caretRect.height > 0)) {
          marker = {
            left: caretRect.left - canvasRect.left - 1,
            top: caretRect.top - canvasRect.top + 1,
            height: Math.max(16, caretRect.height - 2)
          }
          anchorX = caretRect.left - rowRect.left
          anchorY = caretRect.top - rowRect.top + caretRect.height / 2
        }
      }

      if ((!anchorX && !anchorY) || (!rects.length && !marker)) {
        const fallback = block.getBoundingClientRect()
        anchorX = fallback.right - rowRect.left - 8
        anchorY = fallback.top - rowRect.top + fallback.height / 2
      }

      next[edit.id] = { rects, marker, anchorX, anchorY }
    }

    // Balloon layout: place balloons in document coordinate space, then run collision avoidance.
    // DO NOT clamp all balloons into the viewport; that causes heavy overlap when many edits exist.
    const padTop = 12
    const padBottom = 12
    const gap = 12 // 8pt-grid friendly
    const maxY = Math.max(padTop, h - padBottom)

    const getCardHeight = (id: string) => {
      const el = cardElsRef.current.get(id)
      const rect = el?.getBoundingClientRect()
      const hh = rect?.height || 0
      return Math.max(92, Math.min(180, hh || 110))
    }

    const items = edits
      .map((edit) => {
        const anchorY = next[edit.id]?.anchorY || 0
        const height = getCardHeight(edit.id)
        const desiredTop = clamp(anchorY - height / 2, padTop, Math.max(padTop, maxY - height))
        return { id: edit.id, desiredTop, height }
      })
      .sort((a, b) => a.desiredTop - b.desiredTop)

    const placed: Record<string, number> = {}
    let cursor = padTop
    for (const it of items) {
      const top = Math.max(it.desiredTop, cursor)
      placed[it.id] = top
      cursor = top + it.height + gap
    }

    // If we overflow the bottom, shift up with a backward pass.
    const last = items[items.length - 1]
    if (last) {
      const endY = (placed[last.id] ?? padTop) + last.height
      const overflow = endY - (maxY - padBottom)
      if (overflow > 0) {
        for (const it of items) {
          placed[it.id] = Math.max(padTop, (placed[it.id] ?? padTop) - overflow)
        }
        let bottomCursor = maxY - padBottom
        for (let i = items.length - 1; i >= 0; i -= 1) {
          const it = items[i]
          const top = Math.min(placed[it.id] ?? padTop, bottomCursor - it.height)
          placed[it.id] = Math.max(padTop, top)
          bottomCursor = (placed[it.id] ?? padTop) - gap
        }
      }
    }

    setVisuals(next)
    setBalloonTops(placed)
  }

  const measureLinePaths = () => {
    const row = rowRef.current
    const canvas = canvasRef.current
    if (!row) {
      setLinePaths({})
      setTrunkPaths([])
      return
    }
    if (!canvas) {
      setLinePaths({})
      setTrunkPaths([])
      return
    }

    const rowRect = row.getBoundingClientRect()
    const contentRect = canvas.getBoundingClientRect()
    const next: Record<string, string> = {}
    const routes: Array<{ id: string; startX: number; startY: number; endX: number; endY: number; order: number }> = []

    // Keep connector geometry simple:
    // 1) start from the lower edge of the edited highlight,
    // 2) use ONE shared fork point per nearby row group,
    // 3) then a single diagonal segment to each comment card.
    for (let order = 0; order < allEdits.length; order += 1) {
      const edit = allEdits[order]
      const visual = visuals[edit.id]
      const sourceEl = sourceElsRef.current.get(edit.id)
      const cardEl = cardElsRef.current.get(edit.id)
      if (!visual || !cardEl) continue

      const sourceRect = sourceEl?.getBoundingClientRect()
      const cardRect = cardEl.getBoundingClientRect()

      // Start from below the edited location (user expectation).
      const startX = sourceRect ? sourceRect.right - rowRect.left : visual.anchorX
      const startY = sourceRect ? sourceRect.bottom - rowRect.top + 2 : visual.anchorY + 6

      // Card LEFT edge aligns with paper edge (user requirement).
      const endX = cardRect.left - rowRect.left
      const endY = cardRect.top - rowRect.top + cardRect.height / 2

      routes.push({ id: edit.id, startX, startY, endX, endY, order })
    }

    if (routes.length === 0) {
      setLinePaths({})
      setTrunkPaths([])
      return
    }

    const paperRightX = paperEdgeX || contentRect.right - rowRect.left
    const groupThresholdY = 22
    const sourceSorted = routes
      .slice()
      .sort((a, b) => (a.startY === b.startY ? a.startX - b.startX : a.startY - b.startY))

    type RouteGroup = { items: Array<(typeof routes)[number]>; avgStartY: number }
    const groups: RouteGroup[] = []
    for (const route of sourceSorted) {
      const lastGroup = groups[groups.length - 1]
      if (!lastGroup) {
        groups.push({ items: [route], avgStartY: route.startY })
        continue
      }
      const closeY = Math.abs(route.startY - lastGroup.avgStartY) <= groupThresholdY
      if (closeY) {
        lastGroup.items.push(route)
        lastGroup.avgStartY =
          (lastGroup.avgStartY * (lastGroup.items.length - 1) + route.startY) / lastGroup.items.length
      } else {
        groups.push({ items: [route], avgStartY: route.startY })
      }
    }

    const pickMedian = (values: number[]) => values[Math.floor(values.length / 2)] || 0
    const trunks: string[] = []

    for (const group of groups) {
      const groupItems = group.items
      const minCardX = Math.min(...groupItems.map((route) => route.endX))
      const sourceYs = groupItems.map((route) => route.startY).sort((a, b) => a - b)
      const sourceXs = groupItems.map((route) => route.startX).sort((a, b) => a - b)

      // One shared fork point per row-group.
      const forkY = pickMedian(sourceYs)
      const forkX = clamp(paperRightX - 26, (sourceXs[sourceXs.length - 1] || 0) + 28, minCardX - 10)

      // Optional short shared segment to strengthen "merge then split" reading.
      if (groupItems.length > 1) {
        const trunkStartX = clamp(forkX - 28, (sourceXs[sourceXs.length - 1] || 0) + 6, forkX - 6)
        trunks.push(`${trunkStartX},${forkY} ${forkX},${forkY}`)
      }

      for (const route of groupItems) {
        // Single-turn connector: edit anchor -> shared fork -> comment card.
        const sx = route.startX
        const sy = route.startY
        const ex = route.endX
        const ey = route.endY
        next[route.id] = `${sx},${sy} ${forkX},${forkY} ${ex},${ey}`
      }
    }

    setTrunkPaths(trunks)
    setLinePaths(next)
  }

  const scheduleMeasureLinePaths = () => {
    window.requestAnimationFrame(() => {
      measureLinePaths()
    })
  }

  const scheduleCompute = () => {
    if (debounceTimer.current) window.clearTimeout(debounceTimer.current)
    debounceTimer.current = window.setTimeout(() => {
      collectBlocks()
      computeEdits()
    }, 160)
  }

  const scrollToEl = (el: HTMLElement, opts?: { scroll?: boolean; pulse?: boolean }) => {
    const scroll = opts?.scroll !== false
    const pulse = opts?.pulse !== false
    const sc = scrollRef.current
    if (scroll && sc) {
      const rect = el.getBoundingClientRect()
      const scRect = sc.getBoundingClientRect()
      const top = rect.top - scRect.top + sc.scrollTop
      sc.scrollTo({ top: Math.max(0, top - 120), behavior: 'smooth' })
    }
    if (!pulse) return
    if (focusTimer.current) {
      window.clearTimeout(focusTimer.current)
      focusTimer.current = null
    }
    el.classList.remove('focusPulse')
    void el.offsetWidth
    el.classList.add('focusPulse')
    focusTimer.current = window.setTimeout(() => {
      el.classList.remove('focusPulse')
      focusTimer.current = null
    }, 5200)
  }

  const scrollToBlock = (blockId: string) => {
    const el = blockElsRef.current.get(blockId)
    if (el) scrollToEl(el)
  }

  const scrollToEdit = (editId: string) => {
    const sc = scrollRef.current
    const visual = visuals[editId]
    if (sc && visual) {
      sc.scrollTo({ top: Math.max(0, visual.anchorY - 140), behavior: 'smooth' })
    }
    const edit = allEdits.find((item) => item.id === editId)
    if (edit) scrollToBlock(edit.blockId)
  }

  const findBestBlockByText = (inputs: Array<{ text: string; weight: number; allowFragments: boolean }>, allowLoose: boolean) => {
    const normalizeLoose = (text: string) => normalizeLooseSearchText(text)

    const buildCandidates = (text: string, allowFragments: boolean) => {
      const trimmed = (text || '').trim()
      if (!trimmed) return []

      const variants: Array<{ value: string; boost: number }> = [{ value: trimmed, boost: 3.2 }]
      if (allowFragments) {
        const fragments = trimmed
          .split(/[\s，。！？；：、（）【】《》「」『』\[\]{}()<>.,!?;:'"`~!@#$%^&*_\-+=|\\/]+/g)
          .map((part) => part.trim())
          .filter((part) => part.length >= 6)
          .map((part) => ({ value: part, boost: 1.25 }))
        variants.push(...fragments)
        if (trimmed.length >= 28) {
          variants.push({ value: trimmed.slice(0, 28), boost: 1.15 }, { value: trimmed.slice(-28), boost: 1.15 })
        }
      }
      return variants
    }

    type Candidate = { compact: string; loose: string; weight: number; boost: number }
    const byKey = new Map<string, Candidate>()
    for (const input of inputs) {
      const variants = buildCandidates(input.text, input.allowFragments)
      for (const v of variants) {
        const compact = normalizeSearchText(v.value)
        const loose = normalizeLoose(v.value)
        const minLen = input.weight >= 7 ? 4 : 6
        if (compact.length < minLen && loose.length < minLen) continue
        const key = compact ? `c:${compact}` : `l:${loose}`
        const next = { compact, loose, weight: input.weight, boost: v.boost }
        const prev = byKey.get(key)
        if (!prev || next.weight * next.boost > prev.weight * prev.boost) {
          byKey.set(key, next)
        }
      }
    }

    const candidates = Array.from(byKey.values())
    if (candidates.length === 0) return null

    let best: { el: BlockEl; score: number } | null = null
    for (const el of blockElsRef.current.values()) {
      const txt = plainTextOf(el)
      const compactTxt = normalizeSearchText(txt)
      const looseTxt = normalizeLoose(txt)
      for (const candidate of candidates) {
        let score = 0
        if (candidate.compact.length >= 6 && compactTxt.includes(candidate.compact)) {
          score = Math.max(score, candidate.compact.length * candidate.weight * candidate.boost + 12)
        }
        if (allowLoose && candidate.loose.length >= 8 && looseTxt.includes(candidate.loose)) {
          score = Math.max(score, candidate.loose.length * candidate.weight * candidate.boost * 0.72)
        }
        if (!best || score > best.score) {
          best = { el, score }
        }
      }
    }
    if (!best || best.score <= 0) return null
    return best.el
  }

  const locateByText = (inputs: Array<{ text: string; weight: number; allowFragments: boolean }>, allowLoose: boolean) => {
    const best = findBestBlockByText(inputs, allowLoose)
    if (!best) return false
    scrollToEl(best)
    return true
  }

  const applyAiPatch = (opts: {
    patchId?: string | number
    targetText?: string
    revisedText?: string
    preserveRawTarget?: boolean
    anchorText?: string
    evidenceText?: string
    clauseUids?: string[]
    scroll?: boolean
  }) => {
    const patchId = opts.patchId == null ? '' : String(opts.patchId)
    const rawTargetText = String(opts.targetText || '').trim()
    const normalizedRawTargetText = rawTargetText.replace(/\s+/g, ' ').trim()
    const sanitizedTargetText = sanitizeLocatorText(rawTargetText)
    const originalRevisedText = String(opts.revisedText || '').trim()
    const locateHint = getRiskLocateHint(patchId)
    const clauseUids = opts.clauseUids || []
    const hasContextualFallback = Boolean(
      originalRevisedText &&
        (
          clauseUids.length > 0 ||
          opts.anchorText ||
          opts.evidenceText ||
          locateHint?.anchorText ||
          locateHint?.evidenceText ||
          locateHint?.matchedText
        )
    )

    const patchCandidates = Array.from(
      new Map(
        [
          { targetText: normalizedRawTargetText, revisedText: originalRevisedText },
          { targetText: sanitizedTargetText, revisedText: originalRevisedText },
          ...(opts.preserveRawTarget
            ? []
            : (() => {
                const minimized = minimizePatchPair(normalizedRawTargetText || sanitizedTargetText, originalRevisedText)
                return [
                  { targetText: minimized.targetText.trim(), revisedText: minimized.revisedText.trim() },
                  { targetText: sanitizeLocatorText(minimized.targetText).trim(), revisedText: minimized.revisedText.trim() }
                ]
              })())
        ]
          .filter((candidate) => candidate.targetText)
          .map((candidate) => [`${candidate.targetText}@@${candidate.revisedText}`, candidate])
      ).values()
    )

    if (patchCandidates.length === 0 && !hasContextualFallback) return false
    if (patchCandidates.some((candidate) => candidate.targetText === candidate.revisedText)) return false

    if (patchId) {
      const existingRecord = appliedAiPatchMapRef.current.get(patchId)
      if (existingRecord && (existingRecord.originalRevisedText || existingRecord.revisedText) === originalRevisedText) {
        let existingBlock = blockElsRef.current.get(existingRecord.blockId) || null
        if (!existingBlock) {
          for (const candidate of blockElsRef.current.values()) {
            if (findPatchMarkedNodes(candidate, patchId).length > 0) {
              existingBlock = candidate
              break
            }
          }
        }
        if (existingBlock) {
          const currentText = plainTextOf(existingBlock)
          const markedNodes = findPatchMarkedNodes(existingBlock, patchId)
          const alreadyApplied =
            markedNodes.length > 0 ||
            (existingRecord.afterText && currentText === existingRecord.afterText) ||
            (existingRecord.revisedText && currentText.includes(existingRecord.revisedText) && currentText !== existingRecord.beforeText)
          if (alreadyApplied) {
            scrollToEl(existingBlock)
            return true
          }
        }
      }
    }

    type PatchMatch = {
      block: BlockEl
      currentText: string
      currentHtml: string
      targetText: string
      revisedText: string
      candidateRanges: Array<{ start: number; end: number }>
    }

    const findPatchMatch = (): PatchMatch | 'applied' | null => {
      const allBlocks = Array.from(blockElsRef.current.values())
      let orderedBlocks = allBlocks
      const hintedBlock = locateHint ? blockElsRef.current.get(locateHint.blockId || '') || null : null
      const hintedIndex = hintedBlock ? allBlocks.indexOf(hintedBlock) : -1

      const locateInputs = buildLocateInputs({
        targetText: normalizedRawTargetText || sanitizedTargetText || String(locateHint?.matchedText || ''),
        anchorText: String(opts.anchorText || locateHint?.anchorText || ''),
        evidenceText: String(opts.evidenceText || locateHint?.evidenceText || ''),
        clauseUids
      })
      const preferredBlock = findBestBlockByText(locateInputs.strictInputs, false) || findBestBlockByText(locateInputs.fuzzyInputs, true)

      const resolveStructuralIndex = (items: StructuralItem[], block: BlockEl | null) => {
        if (!block || items.length === 0) return -1
        const table = block.closest('table') as HTMLElement | null
        const anchorElement = table || block
        return items.findIndex((item) => item.element === anchorElement)
      }

      const tryApplyTableAppendPatch = () => {
        if (!docRef.current) return false

        const appendCandidates = Array.from(
          new Map(
            patchCandidates
              .map((candidate) => {
                const analysis = analyzeTableAppendPatch(candidate.targetText, candidate.revisedText)
                if (!analysis) return null
                return [`${analysis.anchorPrefix}@@${analysis.tableMarkdown}@@${analysis.insertText}`, analysis] as const
              })
              .filter(Boolean) as Array<readonly [string, NonNullable<ReturnType<typeof analyzeTableAppendPatch>>]>
          ).values()
        )
        if (appendCandidates.length === 0) return false

        const structuralItems = collectStructuralItems(docRef.current)
        if (structuralItems.length === 0) return false

        const hintedStructuralIndex = resolveStructuralIndex(structuralItems, hintedBlock)
        const preferredStructuralIndex = resolveStructuralIndex(structuralItems, preferredBlock)

        let bestMatch:
          | {
              analysis: NonNullable<ReturnType<typeof analyzeTableAppendPatch>>
              tableItem: StructuralItem
              score: number
            }
          | null = null

        for (const analysis of appendCandidates) {
          const tableLoose = normalizeLooseSearchText(analysis.tableMarkdown)
          const prefixLoose = normalizeLooseSearchText(analysis.anchorPrefix)
          if (!tableLoose) continue

          for (const item of structuralItems) {
            if (item.kind !== 'table') continue

            const looseOverlap =
              item.looseText.includes(tableLoose) || tableLoose.includes(item.looseText)
                ? 1
                : Math.max(tableTokenOverlapRatio(analysis.tableMarkdown, item.text), tableTokenOverlapRatio(item.text, analysis.tableMarkdown))
            if (looseOverlap < 0.45) continue

            let score = looseOverlap * 1000
            const prevBlock = previousStructuralItem(structuralItems, item.index, 'block')
            if (prefixLoose && prevBlock?.looseText) {
              if (prevBlock.looseText.includes(prefixLoose) || prefixLoose.includes(prevBlock.looseText)) {
                score += 320
              } else {
                const combinedLoose = normalizeLooseSearchText(`${prevBlock.text}\n${item.text}`)
                if (combinedLoose.includes(prefixLoose)) score += 160
              }
            }

            const referenceIndexes = [hintedStructuralIndex, preferredStructuralIndex].filter((value) => value >= 0)
            if (referenceIndexes.length > 0) {
              const bestDistance = Math.min(...referenceIndexes.map((value) => Math.abs(item.index - value)))
              score += Math.max(0, 180 - bestDistance * 36)
              if (item.index >= Math.min(...referenceIndexes)) score += 48
            }

            if (!bestMatch || score > bestMatch.score) {
              bestMatch = { analysis, tableItem: item, score }
            }
          }
        }

        if (!bestMatch) return false

        const inserted = upsertInsertedParagraph({
          anchorElement: bestMatch.tableItem.element,
          position: 'after',
          text: bestMatch.analysis.insertText,
          patchId: patchId || undefined,
        })
        if (!inserted) return false

        if (patchId) {
          appliedAiPatchMapRef.current.set(patchId, {
            patchId,
            blockId: inserted.insertionBlock.dataset.blockId || '',
            beforeText: inserted.beforeText,
            afterText: inserted.afterText,
            blockHtmlBefore: inserted.beforeHtml,
            blockHtmlAfter: inserted.insertionBlock.innerHTML,
            originalTargetText: rawTargetText || normalizedRawTargetText || sanitizedTargetText || bestMatch.analysis.displayBeforeText,
            targetText: '',
            revisedText: bestMatch.analysis.insertText,
            originalRevisedText,
            startIndex: inserted.beforeText.length,
            endIndex: inserted.beforeText.length,
            keepUnderlinedDigits: false,
          })
        }

        computeEdits()
        scrollToEl(inserted.insertionBlock, { scroll: opts.scroll !== false })
        return true
      }

      if (tryApplyTableAppendPatch()) return 'applied'

      const findMatchInBlocks = (
        blocks: BlockEl[],
        candidates: Array<{ targetText: string; revisedText: string }>
      ): PatchMatch | null => {
        for (const candidate of candidates) {
          for (const el of blocks) {
            const txt = plainTextOf(el)
            if (!txt) continue
            const exactMatches = findAllOccurrences(txt, candidate.targetText)
            const exactRanges = exactMatches.map((idx) => ({ start: idx, end: idx + candidate.targetText.length }))
            if (exactRanges.length > 0) {
              return {
                block: el,
                currentText: txt,
                currentHtml: el.innerHTML,
                targetText: candidate.targetText,
                revisedText: candidate.revisedText,
                candidateRanges: exactRanges
              }
            }
          }
        }

        for (const candidate of candidates) {
          for (const el of blocks) {
            const txt = plainTextOf(el)
            if (!txt) continue
            const compactRanges = findCompactOccurrencesWithRawRange(txt, candidate.targetText)
            if (compactRanges.length > 0) {
              return {
                block: el,
                currentText: txt,
                currentHtml: el.innerHTML,
                targetText: candidate.targetText,
                revisedText: candidate.revisedText,
                candidateRanges: compactRanges
              }
            }
          }
        }

        for (const candidate of candidates) {
          for (const el of blocks) {
            const txt = plainTextOf(el)
            if (!txt) continue
            const looseRanges = findLooseOccurrencesWithRawRange(txt, candidate.targetText)
            if (looseRanges.length > 0) {
              return {
                block: el,
                currentText: txt,
                currentHtml: el.innerHTML,
                targetText: candidate.targetText,
                revisedText: candidate.revisedText,
                candidateRanges: looseRanges
              }
            }
          }
        }

        return null
      }

      if (hintedBlock) {
        const hintedCandidates = Array.from(
          new Map(
            [
              ...patchCandidates,
              ...[locateHint?.targetText, locateHint?.anchorText, locateHint?.evidenceText]
                .map((text) => String(text || '').trim())
                .filter(Boolean)
                .map((text) => ({ targetText: text, revisedText: originalRevisedText }))
            ]
              .filter((candidate) => candidate.targetText)
              .map((candidate) => [`${candidate.targetText}@@${candidate.revisedText}`, candidate])
          ).values()
        )
        const hintedMatch = findMatchInBlocks([hintedBlock], hintedCandidates)
        if (hintedMatch) return hintedMatch

        if (hintedIndex >= 0) {
          const neighborBlocks = allBlocks.filter((_, idx) => Math.abs(idx - hintedIndex) <= 12)
          const nearbyMatch = findMatchInBlocks(neighborBlocks, hintedCandidates)
          if (nearbyMatch) return nearbyMatch
          orderedBlocks = [
            ...neighborBlocks,
            ...allBlocks.filter((_, idx) => Math.abs(idx - hintedIndex) > 12)
          ]
        }
      }

      const hasLocateContext = Boolean(
        opts.anchorText ||
          opts.evidenceText ||
          locateHint?.anchorText ||
          locateHint?.evidenceText ||
          locateHint?.matchedText ||
          clauseUids.length > 0
      )
      if (hasLocateContext && preferredBlock) {
        const preferredOnlyMatch = findMatchInBlocks([preferredBlock], patchCandidates)
        if (preferredOnlyMatch) return preferredOnlyMatch
        orderedBlocks = [preferredBlock, ...allBlocks.filter((el) => el !== preferredBlock)]
      }

      return findMatchInBlocks(orderedBlocks, patchCandidates)
    }

    const matchedPatch = findPatchMatch()
    if (matchedPatch === 'applied') return true
    if (!matchedPatch) {
      const allBlocks = Array.from(blockElsRef.current.values())
      const hintedBlock = locateHint ? blockElsRef.current.get(locateHint.blockId || '') || null : null
      const hintedIndex = hintedBlock ? allBlocks.indexOf(hintedBlock) : -1
      const clauseTexts = clauseUids.map((uid) => props.clauseTextByUid?.[uid] || '').filter(Boolean)
      const hintedBlockText = hintedBlock ? plainTextOf(hintedBlock) : ''
      const sourceHints = [
        ...clauseTexts,
        hintedBlockText,
        String(locateHint?.matchedText || ''),
        String(opts.evidenceText || locateHint?.evidenceText || ''),
        String(opts.anchorText || locateHint?.anchorText || ''),
        normalizedRawTargetText || sanitizedTargetText,
      ]
        .map((text) => String(text || '').trim())
        .filter(Boolean)
      const locateInputs = buildLocateInputs({
        targetText: normalizedRawTargetText || sanitizedTargetText || String(locateHint?.matchedText || ''),
        anchorText: String(opts.anchorText || locateHint?.anchorText || ''),
        evidenceText: String(opts.evidenceText || locateHint?.evidenceText || ''),
        clauseUids,
      })
      const preferredBlock =
        findBestBlockByText(locateInputs.strictInputs, false) ||
        findBestBlockByText(locateInputs.fuzzyInputs, true)
      const preferredIndex = preferredBlock ? allBlocks.indexOf(preferredBlock) : -1

      if ((preferredIndex >= 0 || hintedIndex >= 0) && originalRevisedText) {
        const structuralItems = docRef.current ? collectStructuralItems(docRef.current) : []
        const resolveStructuralIndex = (block: BlockEl | null) => {
          if (!block || structuralItems.length === 0) return -1
          const table = block.closest('table') as HTMLElement | null
          const anchorElement = table || block
          return structuralItems.findIndex((item) => item.element === anchorElement)
        }

        const hintedStructuralIndex = resolveStructuralIndex(hintedBlock)
        const preferredStructuralIndex = resolveStructuralIndex(preferredBlock)
        const structuralStartIndexes = new Set<number>()
        if (hintedStructuralIndex >= 0) {
          for (let idx = Math.max(0, hintedStructuralIndex - 3); idx <= hintedStructuralIndex; idx += 1) {
            structuralStartIndexes.add(idx)
          }
        }
        if (preferredStructuralIndex >= 0) {
          for (let idx = Math.max(0, preferredStructuralIndex - 3); idx <= preferredStructuralIndex; idx += 1) {
            structuralStartIndexes.add(idx)
          }
        }

        let bestPlan: StructuredInsertionPlan | null = null
        if (structuralItems.length > 0 && structuralStartIndexes.size > 0) {
          const orderedStartIndexes = Array.from(structuralStartIndexes).sort((a, b) => a - b)
          for (const sourceHint of sourceHints) {
            for (const startIndex of orderedStartIndexes) {
              const plan = buildStructuredInsertionPlan(structuralItems, startIndex, sourceHint, originalRevisedText)
              if (!plan) continue
              if (!bestPlan || plan.score > bestPlan.score) {
                bestPlan = plan
              }
            }
          }
        }

        if (bestPlan) {
          const inserted = upsertInsertedParagraph({
            anchorElement: bestPlan.anchorItem.element,
            position: bestPlan.insertPosition,
            text: bestPlan.insertText,
            patchId: patchId || undefined,
          })
          if (inserted) {
            if (patchId) {
              appliedAiPatchMapRef.current.set(patchId, {
                patchId,
                blockId: inserted.insertionBlock.dataset.blockId || '',
                beforeText: inserted.beforeText,
                afterText: inserted.afterText,
                blockHtmlBefore: inserted.beforeHtml,
                blockHtmlAfter: inserted.insertionBlock.innerHTML,
                originalTargetText: rawTargetText || normalizedRawTargetText || sanitizedTargetText,
                targetText: '',
                revisedText: bestPlan.insertText,
                originalRevisedText,
                startIndex: inserted.beforeText.length,
                endIndex: inserted.beforeText.length,
                keepUnderlinedDigits: false,
              })
            }

            computeEdits()
            scrollToEl(inserted.insertionBlock, { scroll: opts.scroll !== false })
            return true
          }
        }

        const anchorIndexes = new Set<number>()
        if (hintedIndex >= 0) {
          for (let idx = Math.max(0, hintedIndex - 3); idx <= hintedIndex; idx += 1) {
            anchorIndexes.add(idx)
          }
        }
        if (preferredIndex >= 0) {
          for (let idx = Math.max(0, preferredIndex - 3); idx <= preferredIndex; idx += 1) {
            anchorIndexes.add(idx)
          }
        }
        const startIndexes = Array.from(anchorIndexes).sort((a, b) => a - b)
        for (const sourceHint of sourceHints) {
          let cluster: BlockEl[] = []
          for (const startIndex of startIndexes) {
            cluster = collectSequentialBlockCluster(allBlocks, startIndex, sourceHint)
            if (cluster.length > 0) break
          }
          if (cluster.length === 0) continue

          const clusterText = cluster.map((block) => plainTextOf(block)).join('\n')
          let suffixText = ''
          for (const suffixHint of sourceHints) {
            suffixText = extractAppendOnlySuffixFromSourceHint(suffixHint, originalRevisedText)
            if (suffixText) break
          }
          if (!suffixText) {
            suffixText =
              extractAppendOnlySuffixFromClusterText(clusterText, originalRevisedText) ||
              computeAppendOnlySuffix(sourceHint, originalRevisedText)
          }
          if (!suffixText) continue

          const lastClusterBlock = cluster[cluster.length - 1]
          const tableAncestor = lastClusterBlock.closest('table') as HTMLElement | null
          const inserted = upsertInsertedParagraph({
            anchorElement: tableAncestor || lastClusterBlock,
            position: 'after',
            text: suffixText,
            patchId: patchId || undefined,
          })
          if (!inserted) continue

          if (patchId) {
            appliedAiPatchMapRef.current.set(patchId, {
              patchId,
              blockId: inserted.insertionBlock.dataset.blockId || '',
              beforeText: inserted.beforeText,
              afterText: inserted.afterText,
              blockHtmlBefore: inserted.beforeHtml,
              blockHtmlAfter: inserted.insertionBlock.innerHTML,
              originalTargetText: rawTargetText || normalizedRawTargetText || sanitizedTargetText,
              targetText: '',
              revisedText: suffixText,
              originalRevisedText,
              startIndex: inserted.beforeText.length,
              endIndex: inserted.beforeText.length,
              keepUnderlinedDigits: false,
            })
          }

          computeEdits()
          scrollToEl(inserted.insertionBlock, { scroll: opts.scroll !== false })
          return true
        }
      }

      return false
    }

    const matched = matchedPatch.block
    const currentText = matchedPatch.currentText
    const currentHtml = matchedPatch.currentHtml
    const matchedTargetText = matchedPatch.targetText
    const revisedText = matchedPatch.revisedText
    let nextText = currentText
    let startIndex = -1
    let endIndex = -1
    let keepUnderlinedDigits = false
    let effectiveTargetText = matchedTargetText
    let replaced = false
    const candidateRanges = matchedPatch.candidateRanges

    if (candidateRanges.length > 0) {
      let best = candidateRanges[0]
      let bestUnderline = hasUnderlinedDigitsInRange(matched, best.start, best.end)
      for (const range of candidateRanges.slice(1)) {
        const underlined = hasUnderlinedDigitsInRange(matched, range.start, range.end)
        if (underlined && !bestUnderline) {
          best = range
          bestUnderline = true
        }
      }
      startIndex = best.start
      const adjusted = adjustPatchBoundary(currentText, best.start, best.end, revisedText)
      startIndex = adjusted.effectiveStart
      endIndex = adjusted.effectiveEnd
      effectiveTargetText = adjusted.effectiveTargetText
      keepUnderlinedDigits = bestUnderline
      replaced = replaceTextRangePreserveStyle(matched, startIndex, endIndex, revisedText, keepUnderlinedDigits, patchId || undefined)
      if (replaced) {
        nextText = currentText.slice(0, startIndex) + revisedText + currentText.slice(endIndex)
      }
    }

    if (!replaced) return false
    if (nextText === currentText) return false

    if (patchId) {
      appliedAiPatchMapRef.current.set(patchId, {
        patchId,
        blockId: matched.dataset.blockId || '',
        beforeText: currentText,
        afterText: nextText,
        blockHtmlBefore: currentHtml,
        blockHtmlAfter: matched.innerHTML,
        originalTargetText: rawTargetText || matchedTargetText,
        targetText: effectiveTargetText || matchedTargetText,
        revisedText,
        originalRevisedText,
        startIndex,
        endIndex,
        keepUnderlinedDigits
      })
    }

    computeEdits()
    scrollToEl(matched, { scroll: opts.scroll !== false })
    return true
  }
  const revertAiPatch = (patchId: string | number) => {
    const key = String(patchId || '')
    if (!key) return false
    const record = appliedAiPatchMapRef.current.get(key)
    if (!record) return false

    let block = blockElsRef.current.get(record.blockId)
    if (!block) {
      for (const candidate of blockElsRef.current.values()) {
        const txt = plainTextOf(candidate)
        if (record.afterText && txt.includes(record.afterText)) {
          block = candidate
          break
        }
      }
    }
    if (!block) return false

    const currentText = plainTextOf(block)
    if (currentText === record.beforeText) {
      appliedAiPatchMapRef.current.delete(key)
      computeEdits()
      return true
    }

    let reverted = false

    const markedNodes = findPatchMarkedNodes(block, key)
    const canRestoreBlockSnapshot =
      Boolean(record.blockHtmlBefore) &&
      (markedNodes.length > 0 || currentText === record.afterText || block.innerHTML === record.blockHtmlAfter)

    if (canRestoreBlockSnapshot) {
      block.innerHTML = record.blockHtmlBefore
      block.normalize()
      reverted = true
    }

    if (!reverted && markedNodes.length > 0) {
      const range = document.createRange()
      const first = markedNodes[0]
      const last = markedNodes[markedNodes.length - 1]
      range.setStartBefore(first)
      range.setEndAfter(last)
      const startParent = first.parentElement as Element | null
      const fragment = createReplacementFragment(
        startParent,
        record.targetText || '',
        record.keepUnderlinedDigits
      )
      range.deleteContents()
      range.insertNode(fragment)
      block.normalize()
      reverted = true
    }

    if (!reverted && record.revisedText) {
      const nearStart = record.startIndex >= 0 ? Math.max(0, record.startIndex - 16) : 0
      let revisedStart = currentText.indexOf(record.revisedText, nearStart)
      if (revisedStart < 0) {
        revisedStart = currentText.indexOf(record.revisedText)
      }
      if (revisedStart >= 0) {
        const revisedEnd = revisedStart + record.revisedText.length
        reverted = replaceTextRangePreserveStyle(
          block,
          revisedStart,
          revisedEnd,
          record.targetText || '',
          record.keepUnderlinedDigits
        )
      }
    }

    if (!reverted) return false

    computeEdits()
    scrollToEl(block)
    appliedAiPatchMapRef.current.delete(key)
    return true
  }

  const getAppliedAiPatch = (patchId: string | number): AppliedAiPatchSnapshot | null => {
    const key = String(patchId || '')
    if (!key) return null
    const record = appliedAiPatchMapRef.current.get(key)
    if (!record) return null
    return {
      patchId: key,
      targetText: String(record.targetText || ''),
      revisedText: String(record.originalRevisedText || record.revisedText || '')
    }
  }

  const buildLocateInputs = (opts: { targetText?: string; anchorText?: string; evidenceText?: string; clauseUids?: string[] }) => {
    const clauseUids = opts.clauseUids || []
    const clauseTexts = clauseUids.map((uid) => props.clauseTextByUid?.[uid] || '')
    const clauseIds = clauseUids.map((uid) => (uid.includes('::') ? uid.split('::')[1] : uid))
    const targetText = sanitizeLocatorText(String(opts.targetText || ''))
    const anchorText = sanitizeLocatorText(String(opts.anchorText || ''))
    const evidenceText = sanitizeLocatorText(String(opts.evidenceText || ''))

    const strictInputs = [
      { text: targetText, weight: 10, allowFragments: false },
      { text: anchorText, weight: 8, allowFragments: false },
      { text: evidenceText, weight: 7, allowFragments: false },
      ...clauseTexts.map((text) => ({ text, weight: 4, allowFragments: false })),
      ...clauseIds.map((text) => ({ text, weight: 3, allowFragments: false }))
    ]

    const fuzzyInputs = [
      { text: targetText, weight: 10, allowFragments: true },
      { text: anchorText, weight: 8, allowFragments: true },
      { text: evidenceText, weight: 7, allowFragments: true },
      ...clauseTexts.map((text) => ({ text, weight: 4, allowFragments: true })),
      ...clauseIds.map((text) => ({ text, weight: 3, allowFragments: true }))
    ]

    return { strictInputs, fuzzyInputs, targetText, anchorText, evidenceText }
  }

  const getRiskLocateHint = (riskId?: string | number) => {
    const riskKey = String(riskId ?? '').trim()
    if (!riskKey) return null
    return locatedRiskHintMapRef.current.get(riskKey) || null
  }

  const resolveLocateBlock = (opts: { targetText?: string; anchorText?: string; evidenceText?: string; clauseUids?: string[] }) => {
    const { strictInputs, fuzzyInputs, targetText, anchorText, evidenceText } = buildLocateInputs(opts)
    const matched = findBestBlockByText(strictInputs, false) || findBestBlockByText(fuzzyInputs, true)
    return {
      matched,
      targetText,
      anchorText,
      evidenceText,
      clauseUids: opts.clauseUids || []
    }
  }

  useImperativeHandle(ref, () => ({
    locateRisk: (opts) => {
      const resolved = resolveLocateBlock(opts)
      const best = resolved.matched
      if (!best) {
        alert('未能在当前文档中定位到风险锚点文本：可能已被编辑修改或原文未匹配。')
        return
      }
      const riskKey = String(opts.riskId ?? '').trim()
      if (riskKey) {
        locatedRiskHintMapRef.current.set(riskKey, {
          riskKey,
          blockId: best.dataset.blockId || '',
          targetText: resolved.targetText,
          anchorText: resolved.anchorText,
          evidenceText: resolved.evidenceText,
          matchedText: plainTextOf(best),
          clauseUids: resolved.clauseUids,
          updatedAt: Date.now(),
        })
      }
      scrollToEl(best)
    },
    scrollToBlock,
    scrollToEdit,
    applyAiPatch,
    revertAiPatch,
    getAppliedAiPatch,
    addSuggestionInsertComment: (opts: {
      riskId: string | number
      suggestionText: string
      riskSourceType?: string
      targetText?: string
      anchorText?: string
      evidenceText?: string
      clauseUids?: string[]
      scroll?: boolean
    }) => {
      const riskId = String(opts.riskId || '').trim()
      const suggestionText = String(opts.suggestionText || '').trim()
      if (!riskId || !suggestionText) return false

      const locateHint = getRiskLocateHint(riskId)
      const hintedBlock = locateHint ? blockElsRef.current.get(locateHint.blockId || '') || null : null
      const locateInputs = buildLocateInputs({
        targetText: locateHint?.targetText || opts.targetText,
        anchorText: locateHint?.anchorText || opts.anchorText,
        evidenceText: locateHint?.evidenceText || opts.evidenceText,
        clauseUids: locateHint?.clauseUids?.length ? locateHint.clauseUids : opts.clauseUids,
      })
      const matched = hintedBlock || findBestBlockByText(locateInputs.strictInputs, false) || findBestBlockByText(locateInputs.fuzzyInputs, true)
      if (!matched) return false

      const blockId = matched.dataset.blockId || ''
      if (!blockId) return false

      const currentText = plainTextOf(matched)
      const targetCandidates = [locateInputs.targetText, locateInputs.anchorText, locateInputs.evidenceText].filter(Boolean)
      let startIndex = 0
      for (const target of targetCandidates) {
        const idx = currentText.indexOf(target)
        if (idx >= 0) {
          startIndex = idx
          break
        }
        const compactTarget = normalizeSearchText(target)
        if (compactTarget && normalizeSearchText(currentText).includes(compactTarget)) {
          startIndex = 0
          break
        }
      }

      const nextComment: EditSummary = {
        id: `suggest_insert:${riskId}`,
        blockId,
        type: 'insert',
        insertedText: `建议插入内容：${suggestionText}`.slice(0, 500),
        deletedText: '',
        updatedAt: Date.now(),
        startIndex,
        endIndex: startIndex,
        tagText: '建议插入',
        kind: 'suggest_insert',
        sourceRiskId: riskId
      }

      setManualComments((prev) => {
        const rest = prev.filter((item) => item.sourceRiskId !== riskId)
        return [...rest, nextComment]
      })
      scrollToEl(matched, { scroll: opts.scroll !== false })
      return true
    },
    removeSuggestionInsertComment: (riskId) => {
      const key = String(riskId || '').trim()
      if (!key) return
      setManualComments((prev) => prev.filter((item) => item.sourceRiskId !== key))
    }
  }))

  useEffect(() => {
    let cancelled = false

    const run = async () => {
      setReady(false)
      setVisuals({})
      setBalloonTops({})
      setLinePaths({})
      setTrunkPaths([])
      setManualComments([])
      baselineRef.current = new Map()
      blockElsRef.current = new Map()
      editMapRef.current = new Map()
      cardElsRef.current = new Map()
      sourceElsRef.current = new Map()
      appliedAiPatchMapRef.current = new Map()
      locatedRiskHintMapRef.current = new Map()
      props.onEditsChange([])

      if (!props.file || !docRef.current) {
        setReady(true)
        return
      }

      docRef.current.innerHTML = ''

      try {
        const buf = await props.file.arrayBuffer()
        if (cancelled) return

        await renderAsync(buf, docRef.current, undefined, {
          className: 'docx',
          inWrapper: true,
          ignoreWidth: false,
          ignoreHeight: false,
          useBase64URL: true
        })

        if (cancelled) return
        collectBlocks()
        applyRiskHighlights()
        docRef.current.addEventListener('input', scheduleCompute)
        docRef.current.addEventListener('keyup', scheduleCompute)
        setReady(true)
      } catch (e) {
        if (!cancelled && docRef.current) {
          docRef.current.innerHTML = `<div class="emptyState">DOCX 渲染失败：${String(e)}</div>`
          setReady(true)
        }
      }
    }

    run()

    return () => {
      cancelled = true
      if (docRef.current) {
        docRef.current.removeEventListener('input', scheduleCompute)
        docRef.current.removeEventListener('keyup', scheduleCompute)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.file])

  useEffect(() => {
    applyRiskHighlights()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.riskHighlights])

  useLayoutEffect(() => {
    if (!ready) return
    const raf = window.requestAnimationFrame(() => measureVisuals(allEdits))
    return () => window.cancelAnimationFrame(raf)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allEdits, ready])

  useLayoutEffect(() => {
    if (!ready) return
    const raf = window.requestAnimationFrame(() => measureLinePaths())
    return () => window.cancelAnimationFrame(raf)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allEdits, visuals, balloonTops, ready])

  useEffect(() => {
    const onResize = () => {
      measureVisuals(allEdits)
      measureLinePaths()
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allEdits])

  useEffect(() => {
    const sc = scrollRef.current
    if (!sc) return
    const onScroll = () => measureLinePaths()
    sc.addEventListener('scroll', onScroll)
    return () => sc.removeEventListener('scroll', onScroll)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, allEdits, visuals])

  useEffect(() => {
    blockElsRef.current.forEach((el) => {
      el.setAttribute('contenteditable', props.isInteractionLocked ? 'false' : 'true')
      el.classList.toggle('editableBlock--locked', Boolean(props.isInteractionLocked))
    })
  }, [props.isInteractionLocked])

  useEffect(() => {
    if (!props.isInteractionLocked) return
    const activeEl = document.activeElement as HTMLElement | null
    if (activeEl && docRef.current?.contains(activeEl)) {
      activeEl.blur()
    }
    const selection = window.getSelection()
    if (selection && selection.rangeCount > 0) {
      const anchorNode = selection.anchorNode
      if (anchorNode && docRef.current?.contains(anchorNode)) {
        selection.removeAllRanges()
      }
    }
  }, [props.isInteractionLocked])

  const lockPercent = typeof props.lockProgress === 'number' && Number.isFinite(props.lockProgress)
    ? Math.max(1, Math.min(99, Math.round(props.lockProgress)))
    : null
  const filledProgressSteps = Math.max(
    1,
    Math.min(
      LOCK_PROGRESS_STEP_COUNT,
      Math.ceil((lockPercent ?? 1) / (100 / LOCK_PROGRESS_STEP_COUNT))
    )
  )

  return (
    <div className={props.className}>
      <div className="docViewport">
        <div ref={scrollRef} className={`docScroll ${props.isInteractionLocked ? 'docScroll--locked' : ''}`}>
          {!ready ? <div className="emptyState">正在加载文档…</div> : null}
          <div ref={rowRef} className={`docRow ${hasComments ? 'docRow--withComments' : 'docRow--compact'}`}>
            <div className="docCanvas" ref={canvasRef}>
              <div ref={docRef} />
              <div className="changeOverlay" aria-hidden="true">
                {allEdits.map((edit) => {
                  const visual = visuals[edit.id]
                  if (!visual) return null
                  return (
                    <React.Fragment key={`overlay-${edit.id}`}>
                      {visual.rects.map((rect, index) => (
                        <span
                          key={`${edit.id}-rect-${index}`}
                          ref={(el) => {
                            if (index === visual.rects.length - 1) {
                              if (el) sourceElsRef.current.set(edit.id, el)
                              else sourceElsRef.current.delete(edit.id)
                              scheduleMeasureLinePaths()
                            }
                          }}
                          data-change-id={edit.id}
                          data-anchor={index === visual.rects.length - 1 ? 'source' : undefined}
                          className={`changeHighlight changeHighlight--${edit.type}`}
                          style={{
                            left: `${rect.left}px`,
                            top: `${rect.top}px`,
                            width: `${Math.max(6, rect.width)}px`,
                            height: `${Math.max(18, rect.height)}px`
                          }}
                        />
                      ))}
                      {visual.marker ? (
                        <span
                          ref={(el) => {
                            if (visual.rects.length === 0) {
                              if (el) sourceElsRef.current.set(edit.id, el)
                              else sourceElsRef.current.delete(edit.id)
                              scheduleMeasureLinePaths()
                            }
                          }}
                          data-change-id={edit.id}
                          data-anchor={visual.rects.length === 0 ? 'source' : undefined}
                          className={`changeDeleteMarker changeDeleteMarker--${edit.type}`}
                          style={{
                            left: `${visual.marker.left}px`,
                            top: `${visual.marker.top}px`,
                            height: `${visual.marker.height}px`
                          }}
                        />
                      ) : null}
                    </React.Fragment>
                  )
                })}
              </div>
            </div>

            {allEdits.length > 0 ? (
              <svg className="commentLines" aria-hidden="true" style={{ height: overlayHeight ? `${overlayHeight}px` : '100%' }}>
                {trunkPaths.map((points, index) => (
                  <polyline key={`trunk-${index}`} points={points} className="commentPolyline commentTrunk" />
                ))}
                {allEdits.map((edit) => {
                  const points = linePaths[edit.id]
                  if (!points) return null
                  return <polyline key={`line-${edit.id}`} points={points} className="commentPolyline" />
                })}
              </svg>
            ) : null}

            <div className={`commentGutter ${allEdits.length > 0 ? 'commentGutter--open' : ''}`} style={{ left: `${gutterLeft}px` }}>
              <div className="commentGutterInner" style={{ height: overlayHeight ? `${overlayHeight}px` : undefined }}>
                {allEdits
                  .slice()
                  .sort((a, b) => a.updatedAt - b.updatedAt)
                  .map((edit) => (
                    <button
                      key={edit.id}
                      ref={(el) => {
                        if (el) cardElsRef.current.set(edit.id, el)
                        else cardElsRef.current.delete(edit.id)
                        scheduleMeasureLinePaths()
                      }}
                      className={`commentBalloon commentBalloon--${edit.type}`}
                      onClick={() => scrollToEdit(edit.id)}
                      title="定位到修订位置"
                      style={{ top: `${balloonTops[edit.id] ?? 0}px` }}
                    >
                      <div className="commentBalloonHead">
                        <span className="commentTag">{edit.tagText || (edit.type === 'insert' ? '插入' : edit.type === 'delete' ? '删除' : '替换')}</span>
                        <span className="commentTime">{new Date(edit.updatedAt).toLocaleTimeString()}</span>
                      </div>
                      {edit.deletedText ? <div className="commentText commentDel">删：{edit.deletedText}</div> : null}
                      {edit.insertedText ? (
                        <div className="commentText commentIns">
                          {edit.kind === 'suggest_insert' ? edit.insertedText : `增：${edit.insertedText}`}
                        </div>
                      ) : null}
                    </button>
                  ))}
              </div>
            </div>
          </div>
        </div>
        {props.isInteractionLocked ? (
          <div className="docInteractionMask" aria-live="polite" aria-busy="true">
            <div className="docInteractionMaskViewport">
              <div className="docInteractionMaskCard">
                <div className="docInteractionMaskText" aria-label="正在审核中......">
                  <span className="docInteractionMaskTextLabel">正在审核中</span>
                  <span className="docInteractionMaskEllipsis" aria-hidden="true">
                    {Array.from({ length: 6 }).map((_, idx) => (
                      <span
                        key={`ellipsis-dot-${idx}`}
                        className="docInteractionMaskEllipsisDot"
                        style={{ animationDelay: `${idx * 0.14}s` }}
                      >
                        .
                      </span>
                    ))}
                  </span>
                </div>
                <div
                  className="docInteractionMaskProgress"
                  role="img"
                  aria-label={`审核进度 ${filledProgressSteps}/${LOCK_PROGRESS_STEP_COUNT}`}
                >
                  {Array.from({ length: LOCK_PROGRESS_STEP_COUNT }).map((_, idx) => (
                    <span
                      key={`progress-step-${idx}`}
                      className={`docInteractionMaskProgressDot ${idx < filledProgressSteps ? 'docInteractionMaskProgressDot--filled' : ''}`}
                      aria-hidden="true"
                    />
                  ))}
                </div>
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  )
})
