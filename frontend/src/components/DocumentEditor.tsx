import React, { forwardRef, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { renderAsync } from 'docx-preview'
import DiffMatchPatch from 'diff-match-patch'
import type { EditSummary } from '../types'

function uid() {
  return Math.random().toString(16).slice(2) + Date.now().toString(16)
}

type BlockEl = HTMLElement & { dataset: { blockId?: string } }

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
  startIndex: number
  endIndex: number
  keepUnderlinedDigits: boolean
}

export type AppliedAiPatchSnapshot = {
  patchId: string
  targetText: string
  revisedText: string
}

export type DocumentEditorHandle = {
  locateRisk: (opts: { riskSourceType?: string; targetText?: string; anchorText?: string; evidenceText?: string; clauseUids?: string[] }) => void
  scrollToBlock: (blockId: string) => void
  scrollToEdit: (editId: string) => void
  applyAiPatch: (opts: {
    patchId?: string | number
    targetText?: string
    revisedText?: string
    preserveRawTarget?: boolean
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
      b.setAttribute('contenteditable', 'true')
      b.setAttribute('spellcheck', 'false')
      b.classList.add('editableBlock')
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
    const normalizeLoose = (text: string) =>
      text
        .replace(/\s+/g, '')
        .replace(/[，。！？；：、“”‘’（）【】《》「」『』\[\]{}()<>.,!?;:'"`~!@#$%^&*_\-+=|\\/]/g, '')
        .toLowerCase()

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
    scroll?: boolean
  }) => {
    const patchId = opts.patchId == null ? '' : String(opts.patchId)
    const rawTargetText = String(opts.targetText || '').trim()
    const normalizedRawTargetText = rawTargetText.replace(/\s+/g, ' ').trim()
    const sanitizedTargetText = sanitizeLocatorText(rawTargetText)
    const candidateTargets = Array.from(
      new Set([normalizedRawTargetText, sanitizedTargetText].filter(Boolean))
    )
    const revisedText = String(opts.revisedText || '').trim()
    if (candidateTargets.length === 0) return false
    if (candidateTargets.some((candidate) => candidate === revisedText)) return false

    if (patchId) {
      const existingRecord = appliedAiPatchMapRef.current.get(patchId)
      if (existingRecord && existingRecord.revisedText === revisedText) {
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

    let matched: BlockEl | null = null
    let matchedTargetText = ''
    for (const candidateTarget of candidateTargets) {
      for (const el of blockElsRef.current.values()) {
        const txt = plainTextOf(el)
        if (txt.includes(candidateTarget)) {
          matched = el
          matchedTargetText = candidateTarget
          break
        }
      }
      if (matched) break
    }

    if (!matched) {
      for (const candidateTarget of candidateTargets) {
        const compactTarget = normalizeSearchText(candidateTarget)
        if (!compactTarget) continue
        for (const el of blockElsRef.current.values()) {
          const txt = plainTextOf(el)
          if (normalizeSearchText(txt).includes(compactTarget)) {
            matched = el
            matchedTargetText = candidateTarget
            break
          }
        }
        if (matched) break
      }
    }

    if (!matched || !matchedTargetText) return false

    const currentText = plainTextOf(matched)
    const currentHtml = matched.innerHTML
    const exactMatches = findAllOccurrences(currentText, matchedTargetText)
    let nextText = currentText
    let startIndex = -1
    let endIndex = -1
    let keepUnderlinedDigits = false
    let effectiveTargetText = matchedTargetText
    let replaced = false

    const candidateRanges: Array<{ start: number; end: number }> = exactMatches.map((idx) => ({
      start: idx,
      end: idx + matchedTargetText.length
    }))
    if (candidateRanges.length === 0) {
      candidateRanges.push(...findCompactOccurrencesWithRawRange(currentText, matchedTargetText))
    }

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
      revisedText: String(record.revisedText || '')
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

  useImperativeHandle(ref, () => ({
    locateRisk: (opts) => {
      const { strictInputs, fuzzyInputs } = buildLocateInputs(opts)
      const ok = locateByText(strictInputs, false) || locateByText(fuzzyInputs, true)
      if (!ok) alert('未能在当前文档中定位到风险锚点文本：可能已被编辑修改或原文未匹配。')
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

      const locateInputs = buildLocateInputs(opts)
      const matched =
        findBestBlockByText(locateInputs.strictInputs, false) || findBestBlockByText(locateInputs.fuzzyInputs, true)
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
    return () => {
      if (focusTimer.current) {
        window.clearTimeout(focusTimer.current)
      }
    }
  }, [])

  return (
    <div className={props.className}>
      <div ref={scrollRef} className="docScroll">
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
    </div>
  )
})
