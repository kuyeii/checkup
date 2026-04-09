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

export type DocumentEditorHandle = {
  locateRisk: (opts: { anchorText?: string; evidenceText?: string; clauseUids?: string[] }) => void
  scrollToBlock: (blockId: string) => void
  scrollToEdit: (editId: string) => void
}

function plainTextOf(el: HTMLElement) {
  return (el.textContent || '').replace(/\u00a0/g, ' ')
}

function normalizeSearchText(text: string) {
  return text.replace(/\s+/g, '')
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

export const DocumentEditor = forwardRef<
  DocumentEditorHandle,
  {
    file: File | null
    edits: EditSummary[]
    onEditsChange: (edits: EditSummary[]) => void
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
  const [zoom, setZoom] = useState(1.08)
  const [balloonTops, setBalloonTops] = useState<Record<string, number>>({})
  const [visuals, setVisuals] = useState<Record<string, EditVisual>>({})
  const [linePaths, setLinePaths] = useState<Record<string, string>>({})
  const [trunkPaths, setTrunkPaths] = useState<string[]>([])
  const hasComments = props.edits.length > 0

  const dmp = useMemo(() => new DiffMatchPatch(), [])

  const baselineRef = useRef<Map<string, string>>(new Map())
  const blockElsRef = useRef<Map<string, BlockEl>>(new Map())
  const editMapRef = useRef<Map<string, EditSummary>>(new Map())
  const debounceTimer = useRef<number | null>(null)
  const focusTimer = useRef<number | null>(null)
  const cardElsRef = useRef<Map<string, HTMLButtonElement>>(new Map())
  const sourceElsRef = useRef<Map<string, HTMLElement>>(new Map())

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
        const insertedText = group.insertedText.trim()
        const deletedText = group.deletedText.trim()
        if (!insertedText && !deletedText) continue

        const type: EditSummary['type'] = insertedText && deletedText ? 'replace' : insertedText ? 'insert' : 'delete'
        const key = `${blockId}::${group.startIndex}::${insertedText}::${deletedText}`
        const prev = editMapRef.current.get(key)

        const summary: EditSummary = {
          id: prev?.id || uid(),
          blockId,
          type,
          insertedText: insertedText.slice(0, 160),
          deletedText: deletedText.slice(0, 160),
          updatedAt: prev?.updatedAt || Date.now(),
          startIndex: group.startIndex,
          endIndex: group.startIndex + group.insertedText.length
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

    const raw = edits
      .map((edit) => ({
        id: edit.id,
        top: Math.max(0, (next[edit.id]?.anchorY || 0) - 28),
        anchorX: next[edit.id]?.anchorX || 0
      }))
      .sort((a, b) => (a.top === b.top ? a.anchorX - b.anchorX : a.top - b.top))

    const placed: Record<string, number> = {}
    let lastTop = -Infinity
    for (const item of raw) {
      const top = Math.max(item.top, lastTop + 88)
      placed[item.id] = top
      lastTop = top
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

    for (let order = 0; order < props.edits.length; order += 1) {
      const edit = props.edits[order]
      const visual = visuals[edit.id]
      const sourceEl = sourceElsRef.current.get(edit.id)
      const cardEl = cardElsRef.current.get(edit.id)
      if (!visual || !cardEl) continue

      const sourceRect = sourceEl?.getBoundingClientRect()
      const cardRect = cardEl.getBoundingClientRect()
      const startX = sourceRect ? sourceRect.left - rowRect.left + sourceRect.width / 2 : visual.anchorX
      const startY = sourceRect ? sourceRect.bottom - rowRect.top + 1 : visual.anchorY
      const endX = cardRect.left - rowRect.left
      const endY = cardRect.top - rowRect.top + cardRect.height / 2
      routes.push({ id: edit.id, startX, startY, endX, endY, order })
    }

    if (routes.length === 0) {
      setLinePaths({})
      setTrunkPaths([])
      return
    }

    const routeParams = {
      mergeInset: 26,
      splitRatio: 0.56,
      splitMinGap: 44,
      splitMaxCardProximity: 56,
      splitGroupThresholdX: 72,
      splitGroupThresholdY: 26
    }

    const contentRightX = contentRect.right - rowRect.left
    const sourceSorted = routes
      .slice()
      .sort((a, b) => (a.startY === b.startY ? (a.startX === b.startX ? (a.id === b.id ? a.order - b.order : a.id.localeCompare(b.id)) : a.startX - b.startX) : a.startY - b.startY))

    type RouteGroup = { items: Array<(typeof routes)[number]>; avgStartY: number }
    const groups: RouteGroup[] = []
    for (const route of sourceSorted) {
      const lastGroup = groups[groups.length - 1]
      if (!lastGroup) {
        groups.push({ items: [route], avgStartY: route.startY })
        continue
      }
      const prev = lastGroup.items[lastGroup.items.length - 1]
      const closeY = Math.abs(route.startY - lastGroup.avgStartY) <= routeParams.splitGroupThresholdY
      const closeX = Math.abs(route.startX - prev.startX) <= routeParams.splitGroupThresholdX
      if (closeY && closeX) {
        lastGroup.items.push(route)
        lastGroup.avgStartY =
          (lastGroup.avgStartY * (lastGroup.items.length - 1) + route.startY) / lastGroup.items.length
      } else {
        groups.push({ items: [route], avgStartY: route.startY })
      }
    }

    const pickMedian = (values: number[]) => values[Math.floor((values.length - 1) / 2)] || 0
    const trunks: string[] = []

    for (const group of groups) {
      const groupItems = group.items
      const minCardX = Math.min(...groupItems.map((route) => route.endX))
      const sourceYs = groupItems.map((route) => route.startY).sort((a, b) => a - b)
      const mergeX = contentRightX - routeParams.mergeInset
      const mergeY = pickMedian(sourceYs)
      const splitBaseX =
        contentRightX + Math.max(routeParams.splitMinGap, (minCardX - contentRightX) * routeParams.splitRatio)
      const splitX = Math.min(splitBaseX, minCardX - routeParams.splitMaxCardProximity)
      const splitY = mergeY

      if (groupItems.length > 1) {
        trunks.push(`${mergeX},${mergeY} ${splitX},${splitY}`)
      }

      for (const route of groupItems) {
        const p1 = `${route.startX},${route.startY}`
        const p2 = `${mergeX},${mergeY}`
        const p3 = `${splitX},${splitY}`
        const p4 = `${route.endX},${route.endY}`
        next[route.id] = `${p1} ${p2} ${p3} ${p4}`
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

  const scrollToEl = (el: HTMLElement) => {
    const sc = scrollRef.current
    if (!sc) return
    const rect = el.getBoundingClientRect()
    const scRect = sc.getBoundingClientRect()
    const top = rect.top - scRect.top + sc.scrollTop
    sc.scrollTo({ top: Math.max(0, top - 120), behavior: 'smooth' })
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
    const edit = props.edits.find((item) => item.id === editId)
    if (edit) scrollToBlock(edit.blockId)
  }

  const locateByText = (texts: string[]) => {
    const normalizeLoose = (text: string) =>
      text
        .replace(/\s+/g, '')
        .replace(/[，。！？；：、“”‘’（）【】《》「」『』\[\]{}()<>.,!?;:'"`~!@#$%^&*_\-+=|\\/]/g, '')
        .toLowerCase()

    const buildCandidates = (text: string) => {
      const trimmed = (text || '').trim()
      if (!trimmed) return []

      const fragments = trimmed
        .split(/[，。！？；：、\n\r\t（）【】《》「」『』\[\]{}()<>.,!?;:'"`~!@#$%^&*_\-+=|\\/]+/g)
        .map((part) => part.trim())
        .filter((part) => part.length >= 6)

      const variants = [trimmed, ...fragments]
      if (trimmed.length >= 28) {
        variants.push(trimmed.slice(0, 28), trimmed.slice(-28))
      }
      return Array.from(new Set(variants))
    }

    const candidates = Array.from(new Set(texts.flatMap(buildCandidates)))
      .map((t) => ({ compact: normalizeSearchText(t), loose: normalizeLoose(t) }))
      .filter((t) => t.compact.length >= 4 || t.loose.length >= 4)

    if (candidates.length === 0) return false

    for (const el of blockElsRef.current.values()) {
      const txt = plainTextOf(el)
      const compactTxt = normalizeSearchText(txt)
      const looseTxt = normalizeLoose(txt)
      for (const candidate of candidates) {
        const hitCompact = candidate.compact.length >= 4 && compactTxt.includes(candidate.compact)
        const hitLoose = candidate.loose.length >= 4 && looseTxt.includes(candidate.loose)
        if (hitCompact || hitLoose) {
          scrollToEl(el)
          return true
        }
      }
    }
    return false
  }

  useImperativeHandle(ref, () => ({
    locateRisk: (opts) => {
      const clauseUids = opts.clauseUids || []
      const clauseTexts = clauseUids.map((uid) => props.clauseTextByUid?.[uid] || '')
      const clauseIds = clauseUids.map((uid) => (uid.includes('::') ? uid.split('::')[1] : uid))
      const ok = locateByText([...clauseTexts, opts.anchorText || '', opts.evidenceText || '', ...clauseIds])
      if (!ok) alert('未能在当前文档中定位到风险锚点文本：可能已被编辑修改或原文未匹配。')
    },
    scrollToBlock,
    scrollToEdit
  }))

  useEffect(() => {
    let cancelled = false

    const run = async () => {
      setReady(false)
      setVisuals({})
      setBalloonTops({})
      setLinePaths({})
      setTrunkPaths([])
      baselineRef.current = new Map()
      blockElsRef.current = new Map()
      editMapRef.current = new Map()
      cardElsRef.current = new Map()
      sourceElsRef.current = new Map()
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
    const raf = window.requestAnimationFrame(() => measureVisuals(props.edits))
    return () => window.cancelAnimationFrame(raf)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.edits, zoom, ready])

  useLayoutEffect(() => {
    if (!ready) return
    const raf = window.requestAnimationFrame(() => measureLinePaths())
    return () => window.cancelAnimationFrame(raf)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.edits, visuals, balloonTops, zoom, ready])

  useEffect(() => {
    const onResize = () => {
      measureVisuals(props.edits)
      measureLinePaths()
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.edits])

  useEffect(() => {
    const sc = scrollRef.current
    if (!sc) return
    const onScroll = () => measureLinePaths()
    sc.addEventListener('scroll', onScroll)
    return () => sc.removeEventListener('scroll', onScroll)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, props.edits, visuals])

  useEffect(() => {
    return () => {
      if (focusTimer.current) {
        window.clearTimeout(focusTimer.current)
      }
    }
  }, [])

  return (
    <div className={props.className}>
      <div className="docToolbar">
        <div className="docToolbarLeft">
          <span className="toolbarLabel">缩放</span>
          <button className="iconBtn" onClick={() => setZoom((z) => Math.max(0.7, +(z - 0.1).toFixed(2)))}>
            －
          </button>
          <span className="zoomText">{Math.round(zoom * 100)}%</span>
          <button className="iconBtn" onClick={() => setZoom((z) => Math.min(1.5, +(z + 0.1).toFixed(2)))}>
            ＋
          </button>
        </div>
      </div>

      <div ref={scrollRef} className="docScroll">
        {!ready ? <div className="emptyState">正在加载文档…</div> : null}
        <div ref={rowRef} className={`docRow ${hasComments ? 'docRow--withComments' : 'docRow--compact'}`} style={{ zoom } as React.CSSProperties}>
          <div className="docCanvas" ref={canvasRef}>
            <div ref={docRef} />
            <div className="changeOverlay" aria-hidden="true">
              {props.edits.map((edit) => {
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

          {props.edits.length > 0 ? (
            <svg className="commentLines" aria-hidden="true">
              {trunkPaths.map((points, index) => (
                <polyline key={`trunk-${index}`} points={points} className="commentPolyline commentTrunk" />
              ))}
              {props.edits.map((edit) => {
                const points = linePaths[edit.id]
                if (!points) return null
                return <polyline key={`line-${edit.id}`} points={points} className="commentPolyline" />
              })}
            </svg>
          ) : null}

          <div className={`commentGutter ${props.edits.length > 0 ? 'commentGutter--open' : ''}`}>
            <div className="commentGutterInner">
              {props.edits
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
                      <span className="commentTag">{edit.type === 'insert' ? '插入' : edit.type === 'delete' ? '删除' : '替换'}</span>
                      <span className="commentTime">{new Date(edit.updatedAt).toLocaleTimeString()}</span>
                    </div>
                    {edit.deletedText ? <div className="commentText commentDel">删：{edit.deletedText}</div> : null}
                    {edit.insertedText ? <div className="commentText commentIns">增：{edit.insertedText}</div> : null}
                  </button>
                ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
})
