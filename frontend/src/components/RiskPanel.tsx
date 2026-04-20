import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import DiffMatchPatch from 'diff-match-patch'
import type { ReviewResultPayload, RiskItem } from '../types'

function levelLabel(level: string) {
  if (level === 'high') return '高'
  if (level === 'medium') return '中'
  if (level === 'low') return '低'
  return level
}

function isAcceptedRiskStatus(status?: string) {
  const normalized = String(status || '').trim().toLowerCase()
  return normalized === 'accepted' || normalized === 'ai_applied'
}

function stripRuleCodes(text?: string) {
  return (text || '')
    .replace(/[【\[][^【】\[\]\n]{0,160}(?:RULE|TPL|POLICY|CHECK|REG|MODEL|STD|CLAUSE)_[^【】\[\]\n]{1,160}[】\]]\s*/g, '')
    .replace(/(?:^|\s)(?:RULE|TPL|POLICY|CHECK|REG|MODEL|STD|CLAUSE)_[A-Za-z0-9_-]+(?=\s|$)/g, ' ')
    .replace(/segment_[A-Za-z0-9_-]+::[A-Za-z0-9_.()（）-]+/g, ' ')
    .replace(/(?:条款|条文|clause)\s*[0-9]+(?:\.[A-Za-z0-9]+)+/gi, ' ')
    .replace(/\b[0-9]+(?:\.[A-Za-z][A-Za-z0-9]*)+\b/g, ' ')
    .replace(/\s{2,}/g, ' ')
    .replace(/([。！？])\s*；+/g, '$1')
    .replace(/；+\s*([。！？])/g, '$1')
    .replace(/；{2,}/g, '；')
    .trim()
}

const CLAUSE_UID_PATTERN = /^segment_[A-Za-z0-9_-]+::[A-Za-z0-9_.()（）-]+$/
const CLAUSE_REF_TOKEN_PATTERN = '[0-9一二三四五六七八九十百千万零〇]+(?:\\.[A-Za-z0-9]+)*'
const LEADING_CLAUSE_LABEL_PATTERNS = [
  new RegExp(`^\\s*(?:条款|条文|clause)\\s*${CLAUSE_REF_TOKEN_PATTERN}\\s*[:：，,]\\s*`, 'iu'),
  new RegExp(`^\\s*第?\\s*${CLAUSE_REF_TOKEN_PATTERN}\\s*(?:条|款)\\s*[:：，,]?\\s*`, 'u'),
  new RegExp(`^\\s*${CLAUSE_REF_TOKEN_PATTERN}\\s*[:：，,]\\s*`, 'u'),
  /^\s*[A-Za-z]+[0-9][A-Za-z0-9]*\s*[:：，,]\s*/u,
]

function stripLeadingClauseLabel(value: string) {
  let cleaned = String(value || '').trim()
  let changed = true
  while (cleaned && changed) {
    changed = false
    for (const pattern of LEADING_CLAUSE_LABEL_PATTERNS) {
      const next = cleaned.replace(pattern, '').trim()
      if (next !== cleaned) {
        cleaned = next
        changed = true
        break
      }
    }
  }
  return cleaned
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

function sanitizeAiTargetText(value?: string) {
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

function isAggregateRiskLike(risk: Partial<RiskItem> | null | undefined) {
  if (!risk) return false
  const ai = (risk.ai_rewrite || risk.ai_apply || {}) as any
  return (
    Boolean(String((risk as any).aggregate_id || '').trim()) ||
    String(ai.workflow_kind || '').trim().toLowerCase() === 'aggregate' ||
    String(risk.risk_source_type || '').trim().toLowerCase() === 'anchored_multi_clause'
  )
}

function normalizePatchTargetForRisk(risk: Partial<RiskItem> | null | undefined, value?: string) {
  const raw = String(value || '').trim()
  if (!raw) return ''
  if (isAggregateRiskLike(risk)) return raw
  return sanitizeAiTargetText(raw)
}

function sanitizeAiCommentText(value?: string) {
  return String(value || '')
    .replace(/segment_[A-Za-z0-9_-]+::[A-Za-z0-9_.()（）-]+/g, '')
    .replace(/(?:条款|条文|clause)\s*[0-9]+(?:\.[A-Za-z0-9]+)+/gi, ' ')
    .replace(/\b[0-9]+(?:\.[A-Za-z][A-Za-z0-9]*)+\b/g, ' ')
    .replace(/\s{2,}/g, ' ')
    .replace(/([。！？])\s*；+/g, '$1')
    .replace(/；+\s*([。！？])/g, '$1')
    .replace(/；{2,}/g, '；')
    .trim()
}

function presentRiskLabel(r: RiskItem) {
  return stripRuleCodes(r.risk_label || r.dimension || '风险项')
}

function suggestionInsertTextOf(r: Partial<RiskItem> | null | undefined) {
  if (!r) return ''
  return (
    stripRuleCodes(r.suggestion) ||
    stripRuleCodes(r.suggestion_optimized) ||
    stripRuleCodes(r.suggestion_minimal) ||
    stripRuleCodes(r.basis) ||
    ''
  )
}

function primaryClauseUidOf(r: Partial<RiskItem> | null | undefined) {
  if (!r) return ''
  const fromClauseUids = Array.isArray(r.clause_uids) ? r.clause_uids : []
  const fromRelatedClauseUids = Array.isArray(r.related_clause_uids) ? r.related_clause_uids : []
  const uid = String(fromClauseUids[0] || fromRelatedClauseUids[0] || r.clause_uid || '').trim()
  return uid
}

function primarySortAnchorRefOf(r: Partial<RiskItem> | null | undefined) {
  if (!r) return ''
  const relatedClauseIds = Array.isArray(r.related_clause_ids) ? r.related_clause_ids : []
  const relatedClauseUids = Array.isArray(r.related_clause_uids) ? r.related_clause_uids : []
  const clauseUids = Array.isArray(r.clause_uids) ? r.clause_uids : []
  const displayClauseIds = Array.isArray(r.display_clause_ids) ? r.display_clause_ids : []
  return String(
    relatedClauseIds[0] ||
      relatedClauseUids[0] ||
      clauseUids[0] ||
      r.clause_id ||
      displayClauseIds[0] ||
      r.clause_uid ||
      ''
  ).trim()
}

function showAcceptError(error: unknown, fallbackPrefix: string) {
  const msg = String((error as any)?.message || error || '').trim()
  alert(`${fallbackPrefix}${msg || '请求失败'}`)
}

type DiffSegment = {
  kind: 'equal' | 'delete' | 'insert'
  text: string
}

function buildDiffSegments(before: string, after: string): DiffSegment[] {
  const dmp = new DiffMatchPatch()
  const diffs = dmp.diff_main(before, after)
  dmp.diff_cleanupSemantic(diffs)
  const segments: DiffSegment[] = []
  for (const [op, text] of diffs) {
    if (!text) continue
    if (op === 0) segments.push({ kind: 'equal', text })
    else if (op === -1) segments.push({ kind: 'delete', text })
    else if (op === 1) segments.push({ kind: 'insert', text })
  }
  return segments
}

function AiRewriteDiff(props: { targetText: string; revisedText: string }) {
  const targetText = String(props.targetText || '')
  const revisedText = String(props.revisedText || '')
  const segments = useMemo(() => buildDiffSegments(targetText, revisedText), [targetText, revisedText])
  const showDetails =
    targetText.length > 180 || revisedText.length > 180 || targetText.includes('\n') || revisedText.includes('\n')

  const renderLine = (label: string, mode: 'before' | 'after') => (
    <div className="aiRewriteRow">
      <div className="aiRewriteLabel">{label}</div>
      <div className="aiRewriteText">
        {segments.map((seg, idx) => {
          if (seg.kind === 'equal') {
            return <span key={`${mode}-eq-${idx}`}>{seg.text}</span>
          }
          if (mode === 'before' && seg.kind === 'delete') {
            return (
              <span key={`${mode}-del-${idx}`} className="diffDel">
                {seg.text}
              </span>
            )
          }
          if (mode === 'after' && seg.kind === 'insert') {
            return (
              <span key={`${mode}-ins-${idx}`} className="diffIns">
                {seg.text}
              </span>
            )
          }
          return null
        })}
      </div>
    </div>
  )

  return (
    <div className="aiRewriteBox">
      {renderLine('原文', 'before')}
      {renderLine('改写后', 'after')}
      {showDetails ? (
        <details className="aiRewriteDetails">
          <summary>查看全文</summary>
          <div className="aiRewriteFull">
            <div className="aiRewriteRow">
              <div className="aiRewriteLabel">原文</div>
              <div className="aiRewriteText">{targetText}</div>
            </div>
            <div className="aiRewriteRow">
              <div className="aiRewriteLabel">改写后</div>
              <div className="aiRewriteText">{revisedText}</div>
            </div>
          </div>
        </details>
      ) : null}
    </div>
  )
}

function EditorSheet(props: {
  open: boolean
  targetText: string
  draftText: string
  onDraftChange: (value: string) => void
  onCancel: () => void
  onSave: () => Promise<void>
}) {
  const [saving, setSaving] = useState(false)
  if (!props.open) return null

  return (
    <div className="editorOverlay" onClick={props.onCancel}>
      <div className="editorSheet" onClick={(e) => e.stopPropagation()}>
        <div className="editorHeader">
          <div className="editorTitle">编辑 AI 建议</div>
          <div className="editorActions">
            <button className="btnGhost" onClick={props.onCancel} disabled={saving}>
              取消
            </button>
            <button
              className="btnPrimarySolid"
              disabled={saving || !props.draftText.trim()}
              onClick={async () => {
                try {
                  setSaving(true)
                  await props.onSave()
                } finally {
                  setSaving(false)
                }
              }}
            >
              保存
            </button>
          </div>
        </div>
        <div className="editorBody">
          <div className="editorField">
            <div className="editorLabel">原文</div>
            <div className="editorReadonly">{props.targetText || '—'}</div>
          </div>
          <div className="editorField">
            <div className="editorLabel">改写后</div>
            <textarea
              className="editorTextarea"
              value={props.draftText}
              onChange={(e) => props.onDraftChange(e.target.value)}
              placeholder="请输入改写后的文本"
            />
          </div>
          <div className="editorHelper">保存后可用于参考，不会自动写入导出批注</div>
          <div className="editorCounter">{props.draftText.length} 字</div>
        </div>
      </div>
    </div>
  )
}

export function RiskPanel(props: {
  result: ReviewResultPayload | null
  runId?: string | null
  riskStats?: { total: number; high: number; medium: number; low: number }
  onLocateRisk: (opts: { riskId?: number | string; riskSourceType?: string; targetText?: string; anchorText?: string; evidenceText?: string; clauseUids?: string[] }) => void
  onAcceptRisk?: (riskId: number | string, opts?: { revisedText?: string }) => Promise<void>
  onRejectRisk?: (riskId: number | string) => Promise<void>
  onSetRiskStatus?: (riskId: number | string, status: 'pending' | 'accepted' | 'rejected') => Promise<void>
  onAcceptAllRisks?: () => Promise<void>
  onUndoAcceptAllRisks?: () => Promise<void>
  canUndoAcceptAllRisks?: boolean

  /** Legacy APIs (old backend) */
  onAiApplyRisk?: (riskId: number | string) => Promise<void>

  /** New AI rewrite APIs (latest backend) */
  onAiAcceptRisk?: (riskId: number | string, revisedText?: string) => Promise<void>
  onAiEditRisk?: (riskId: number | string, revisedText: string) => Promise<void>
  onAiRejectRisk?: (riskId: number | string) => Promise<void>
}) {
  const [editorRiskId, setEditorRiskId] = useState<string>('')
  const [editorTargetText, setEditorTargetText] = useState('')
  const [editorDraftText, setEditorDraftText] = useState('')
  const [editorMode, setEditorMode] = useState<'ai' | 'suggest'>('ai')
  const [localDraftById, setLocalDraftById] = useState<Record<string, string>>({})
  const [lastAction, setLastAction] = useState<{ riskId: string; riskLabel: string; action: 'accepted' | 'rejected' } | null>(null)
  const riskListRef = useRef<HTMLDivElement | null>(null)
  const pendingRiskListScrollTopRef = useRef<number | null>(null)

  const snapshotRiskListScroll = () => {
    pendingRiskListScrollTopRef.current = riskListRef.current?.scrollTop ?? null
  }

  useLayoutEffect(() => {
    if (pendingRiskListScrollTopRef.current == null || !riskListRef.current) return
    riskListRef.current.scrollTop = pendingRiskListScrollTopRef.current
    pendingRiskListScrollTopRef.current = null
  })

  // Persist edited AI suggestions locally so reopening history does NOT lose user edits.
  // This also provides a safe fallback when the backend doesn't support /ai_edit.
  useEffect(() => {
    const rid = props.runId
    if (!rid) return
    try {
      const raw = localStorage.getItem(`aiDraft:${rid}`)
      if (!raw) return
      const parsed = JSON.parse(raw) as Record<string, string>
      if (parsed && typeof parsed === 'object') setLocalDraftById(parsed)
    } catch {
      // ignore
    }
  }, [props.runId])

  const persistLocalDraft = (next: Record<string, string>) => {
    const rid = props.runId
    if (!rid) return
    try {
      localStorage.setItem(`aiDraft:${rid}`, JSON.stringify(next))
    } catch {
      // ignore
    }
  }

  const readPersistedDraft = (riskId: string) => {
    const rid = props.runId
    if (!rid) return ''
    try {
      const raw = localStorage.getItem(`aiDraft:${rid}`)
      if (!raw) return ''
      const parsed = JSON.parse(raw) as Record<string, string>
      const val = parsed?.[riskId]
      return typeof val === 'string' ? val : ''
    } catch {
      return ''
    }
  }

  const risks = (props.result?.risk_result_validated?.risk_result?.risk_items || []).filter((r) => {
    const st = String(r.status || 'pending')
    return st === 'pending' || st === ''
  })
  const acceptedCount = useMemo(() => {
    const items = props.result?.risk_result_validated?.risk_result?.risk_items || []
    return items.filter((r) => isAcceptedRiskStatus(String(r.status || ''))).length
  }, [props.result])

  const clauseOrder = useMemo(() => {
    const map = new Map<string, number>()
    for (const [idx, clause] of (props.result?.merged_clauses || []).entries()) {
      const refs = [
        String(clause?.clause_uid || '').trim(),
        String(clause?.clause_id || '').trim(),
        String(clause?.display_clause_id || '').trim()
      ].filter(Boolean)
      for (const ref of refs) {
        if (map.has(ref)) continue
        map.set(ref, idx)
      }
    }
    return map
  }, [props.result])

  const grouped = useMemo(() => {
    const map = new Map<string, RiskItem[]>()
    for (const r of risks) {
      const key = r.dimension || '未分类'
      const list = map.get(key) || []
      list.push(r)
      map.set(key, list)
    }

    const riskAnchorOrder = (risk: RiskItem) => {
      const anchorRef = primarySortAnchorRefOf(risk)
      if (anchorRef && clauseOrder.has(anchorRef)) return clauseOrder.get(anchorRef) as number
      return Number.MAX_SAFE_INTEGER
    }

    const groups = Array.from(map.entries()).map(([dim, items]) => {
      const sortedItems = items.slice().sort((a, b) => {
        const oa = riskAnchorOrder(a)
        const ob = riskAnchorOrder(b)
        if (oa !== ob) return oa - ob
        return String(a.risk_id).localeCompare(String(b.risk_id), 'zh-Hans-CN', { numeric: true })
      })
      const minOrder = sortedItems.length > 0 ? riskAnchorOrder(sortedItems[0]) : Number.MAX_SAFE_INTEGER
      return { dim, items: sortedItems, minOrder }
    })

    groups.sort((a, b) => {
      if (a.minOrder !== b.minOrder) return a.minOrder - b.minOrder
      return a.dim.localeCompare(b.dim)
    })

    return groups.map((g) => [g.dim, g.items] as [string, RiskItem[]])
  }, [risks, clauseOrder])

  const locatePayloadOf = (r: RiskItem) => {
    const primaryUid = primaryClauseUidOf(r)
    return {
      riskId: r.risk_id,
      targetText: normalizePatchTargetForRisk(r, String((r.ai_rewrite || r.ai_apply || (r as any))?.target_text || '')),
      anchorText: sanitizeAiTargetText(r.anchor_text || ''),
      evidenceText: sanitizeAiTargetText(r.evidence_text || ''),
      clauseUids: primaryUid ? [primaryUid] : []
    }
  }

  const stats = useMemo(() => {
    const fromProps = props.riskStats
    if (fromProps) return fromProps
    const allItems = props.result?.risk_result_validated?.risk_result?.risk_items || []
    const c = { total: allItems.length, high: 0, medium: 0, low: 0 }
    for (const r of allItems) {
      if (r.risk_level === 'high') c.high += 1
      else if (r.risk_level === 'medium') c.medium += 1
      else if (r.risk_level === 'low') c.low += 1
    }
    return c
  }, [props.riskStats, props.result])

  return (
    <div className="riskRoot">
      <div className="paneHeader paneHeader--risk">
        <div className="paneTitle">风险点</div>
        <div className="riskHeaderStats">
          <span className="riskHeaderStat">总 {stats.total}</span>
          <span className="riskHeaderStat riskHeaderStat--high">高 {stats.high}</span>
          <span className="riskHeaderStat riskHeaderStat--medium">中 {stats.medium}</span>
          <span className="riskHeaderStat riskHeaderStat--low">低 {stats.low}</span>
        </div>
      </div>

      {!props.result ? (
        <div className="riskEmptyState">请先在左侧进入“文件上传”，开始新的合同审查。</div>
      ) : (
        <>
          {lastAction && props.onSetRiskStatus ? (
            <div className="riskControls">
              <div className="riskUndoBar">
                <div className="riskUndoBarText">
                  <span className="riskUndoBarTitle">{lastAction.riskLabel}</span>
                  <span className="riskUndoBarMeta">{lastAction.action === 'accepted' ? '已接受 1 条风险点' : '已拒绝 1 条风险点'}</span>
                </div>
                <button
                  className="btnSmall"
                  onClick={async () => {
                    try {
                      snapshotRiskListScroll()
                      await props.onSetRiskStatus?.(lastAction.riskId, 'pending')
                      setLastAction(null)
                    } catch (e) {
                      alert(`撤销失败：${String(e)}`)
                    }
                  }}
                >
                  撤销
                </button>
              </div>
            </div>
          ) : null}

          <div className="riskList" ref={riskListRef}>
            {grouped.map(([dim, items]) => (
              <details key={dim} className="riskGroup" open>
                <summary className="riskGroupTitle">
                  <span>{dim}</span>
                  <span className="riskGroupCount">{items.length}</span>
                </summary>
                <div className="riskCards">
                  {items
                    .map((r) => (
                      <div key={r.risk_id} className="riskCard">
                        <div className="riskCardHead">
                          <div className="riskTitle">
                            <span className={`riskBadge riskBadge--${r.risk_level}`}>{levelLabel(String(r.risk_level))}</span>
                            <span className="riskLabel">{presentRiskLabel(r)}</span>
                          </div>
                        </div>

                        <div className="riskSection">
                          <div className="riskSectionTitle">问题</div>
                          <div className="riskSectionBody">{stripRuleCodes(r.issue)}</div>
                        </div>
                        <div className="riskSection">
                          <div className="riskSectionTitle">依据</div>
                          <div className="riskSectionBody">
                            <div>{stripRuleCodes(r.basis) || '—'}</div>
                            <div style={{ marginTop: 8 }}>建议：{suggestionInsertTextOf(r) || '—'}</div>
                          </div>
                        </div>

                        {(() => {
                          const ai = r.ai_rewrite || r.ai_apply
                          const decision = r.ai_rewrite ? r.ai_rewrite_decision : (r.status === 'rejected' ? 'rejected' : undefined)
                          const localKey = String(r.risk_id)
                          const suggestionInsertText = suggestionInsertTextOf(r)
                          const effectiveRevised = (localDraftById[localKey] ?? ai?.revised_text ?? '') as string
                          const effectiveTarget = normalizePatchTargetForRisk(r, String((ai as any)?.target_text || ''))

                          // Legacy backend sometimes returns ai_apply without a `state` field.
                          // If we have revised_text, we treat it as succeeded.
                          const aiState = (ai as any)?.state || (effectiveRevised ? 'succeeded' : undefined)

                          if (!ai) {
                            const effectiveSuggestion = String(localDraftById[localKey] ?? suggestionInsertText ?? '—')
                            return (
                              <div className="riskSection">
                                <div className="riskSectionTitle">AI 建议内容</div>
                                <div className="riskSectionBody">建议插入内容：{effectiveSuggestion}</div>
                              </div>
                            )
                          }

                          if (decision === 'rejected') {
                            return (
                              <div className="riskSection">
                                <div className="riskSectionTitle">AI 建议内容</div>
                                <div className="riskSectionBody">已拒绝 AI 建议</div>
                              </div>
                            )
                          }

                          if (aiState === 'failed') {
                            return (
                              <div className="riskSection">
                                <div className="riskSectionTitle">AI 建议内容</div>
                                <div className="riskSectionBody">AI 建议生成失败</div>
                              </div>
                            )
                          }

                          if (aiState !== 'succeeded') {
                            return (
                              <div className="riskSection">
                                <div className="riskSectionTitle">AI 建议内容</div>
                                <div className="riskSectionBody">AI 建议生成中...</div>
                              </div>
                            )
                          }

                          return (
                            <div className="riskSection">
                              <div className="riskSectionTitle">AI 建议内容</div>
                              <div className="riskSectionBody">{sanitizeAiCommentText((ai as any)?.comment_text) || '—'}</div>
                              <AiRewriteDiff
                                targetText={effectiveTarget}
                                revisedText={String(effectiveRevised || '')}
                              />
                            </div>
                          )
                        })()}

                                                <div className="riskCardActions">
                          <button
                            className="btnSmall"
                            onClick={() => props.onLocateRisk(locatePayloadOf(r))}
                          >
                            定位原文
                          </button>

                          <button
                            className="btnSmall"
                            // Allow editing for succeeded AI suggestions, including delete-style rewrites whose revised_text is empty.
                            disabled={
                              !(
                                String((r.ai_rewrite || r.ai_apply)?.state || '').toLowerCase() === 'succeeded' ||
                                Boolean(
                                  localDraftById[String(r.risk_id)] ??
                                    (r.ai_rewrite || r.ai_apply)?.revised_text ??
                                    suggestionInsertTextOf(r)
                                )
                              )
                            }
                            onClick={() => {
                              props.onLocateRisk(locatePayloadOf(r))
                              const ai = r.ai_rewrite || r.ai_apply
                              setEditorRiskId(String(r.risk_id))
                              const localKey = String(r.risk_id)
                              if (ai) {
                                setEditorMode('ai')
                                setEditorTargetText(normalizePatchTargetForRisk(r, String((ai as any)?.target_text || '')))
                                setEditorDraftText(String(localDraftById[localKey] ?? (ai as any)?.revised_text ?? ''))
                              } else {
                                setEditorMode('suggest')
                                setEditorTargetText('建议插入内容')
                                setEditorDraftText(String(localDraftById[localKey] ?? suggestionInsertTextOf(r) ?? ''))
                              }
                            }}
                          >
                            修改
                          </button>

                          <button
                            className="btnSmall"
                            disabled={r.risk_id === undefined || r.risk_id === null}
                            onClick={async () => {
                              if (r.risk_id === undefined || r.risk_id === null) return
                              try {
                                snapshotRiskListScroll()
                                await props.onRejectRisk?.(r.risk_id)
                                setLastAction({ riskId: String(r.risk_id), riskLabel: presentRiskLabel(r), action: 'rejected' })
                              } catch (e) {
                                console.error('拒绝失败', e)
                                alert(`拒绝失败：${String(e)}`)
                              }
                            }}
                          >
                            拒绝
                          </button>

                          <button
                            className="btnSmall btnSmall--primary"
                            disabled={r.risk_id === undefined || r.risk_id === null}
                            onClick={async () => {
                              if (r.risk_id === undefined || r.risk_id === null) return
                              snapshotRiskListScroll()
                              const ai = r.ai_rewrite || r.ai_apply
                              const localKey = String(r.risk_id)
                              const effectiveRevised = String(
                                localDraftById[localKey] ?? (ai as any)?.revised_text ?? suggestionInsertTextOf(r) ?? ''
                              ).trim()
                              try {
                                await props.onAcceptRisk?.(r.risk_id, { revisedText: effectiveRevised || undefined })
                                setLastAction({ riskId: String(r.risk_id), riskLabel: presentRiskLabel(r), action: 'accepted' })
                              } catch (e) {
                                console.error('接受失败', e)
                                showAcceptError(e, '接受失败：')
                              }
                            }}
                          >
                            接受
                          </button>
                        </div>
                      </div>
                    ))}
                </div>
              </details>
            ))}
            {grouped.length === 0 ? (
              <div className="riskEmptyState">
                <div className="riskEmptyStateTitle">
                  {acceptedCount > 0 ? '当前没有待处理风险点' : '当前筛选条件下没有风险项。'}
                </div>
                {acceptedCount > 0 ? (
                  <>
                    <div className="riskEmptyStateDesc">本次风险已全部接受。如需继续调整，可以先恢复为待处理，再重新逐条处理。</div>
                    {props.onUndoAcceptAllRisks ? (
                      <button
                        className="btnSmall btnSmall--primary"
                        disabled={!props.canUndoAcceptAllRisks}
                        onClick={async () => {
                          try {
                            snapshotRiskListScroll()
                            await props.onUndoAcceptAllRisks?.()
                          } catch (e) {
                            alert(`恢复失败：${String(e)}`)
                          }
                        }}
                      >
                        恢复全部为待处理
                      </button>
                    ) : null}
                  </>
                ) : null}
              </div>
            ) : null}
          </div>
        </>
      )}

      <EditorSheet
        open={Boolean(editorRiskId)}
        targetText={editorTargetText}
        draftText={editorDraftText}
        onDraftChange={setEditorDraftText}
        onCancel={() => {
          setEditorRiskId('')
          setEditorTargetText('')
          setEditorDraftText('')
          setEditorMode('ai')
        }}
        onSave={async () => {
          if (!editorRiskId) return

          // Always persist locally first so the UI updates instantly.
          const nextLocal = { ...localDraftById, [String(editorRiskId)]: editorDraftText }
          setLocalDraftById(nextLocal)
          persistLocalDraft(nextLocal)

          // Then try backend edit endpoint (optional). If 404, keep local only.
          if (editorMode === 'ai' && props.onAiEditRisk) {
            try {
              await props.onAiEditRisk(editorRiskId, editorDraftText)
            } catch (e: any) {
              const msg = String(e?.message || e)
              const code = (e as any)?.code
              if (!(code === 404 || msg.includes('404') || msg.includes('Not Found'))) {
                alert(`保存失败：${msg}`)
              }
            }
          }

          setEditorRiskId('')
          setEditorTargetText('')
          setEditorDraftText('')
          setEditorMode('ai')
        }}
      />
    </div>
  )
}
