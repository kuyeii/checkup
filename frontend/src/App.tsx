import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { DocumentEditor, DocumentEditorHandle } from './components/DocumentEditor'
import { ReviewHistoryPanel } from './components/ReviewHistoryPanel'
import { RiskPanel } from './components/RiskPanel'
import { SideNav, type NavKey } from './components/SideNav'
import { ModernSideNav } from './components/ModernSideNav'
import { TopBar } from './components/TopBar'
import { GlobalTopBar } from './components/GlobalTopBar'
import { UploadDashboard } from './components/UploadDashboard'
import { ReviewProgress } from './components/ReviewProgress'
import type { EditSummary, ReviewHistoryItem, ReviewMeta, ReviewResultPayload, ReviewSideOption } from './types'

async function sleep(ms: number) {
  await new Promise((r) => setTimeout(r, ms))
}

async function readErrorDetail(resp: Response) {
  const text = await resp.text()
  if (!text) return '请求失败'
  try {
    const parsed = JSON.parse(text) as { detail?: string }
    const detail = String(parsed?.detail || '').trim()
    if (detail) return detail
  } catch {
    // ignore parse error and use raw text
  }
  return text
}

function fetchNoStore(input: RequestInfo | URL, init?: RequestInit) {
  return fetch(input, { cache: 'no-store', ...init })
}

function resolveHistoryUpdatedAt(params: {
  status?: string | null
  remoteUpdatedAt?: string | null
  previousUpdatedAt?: string | null
  fallbackNow?: boolean
}) {
  const { status, remoteUpdatedAt, previousUpdatedAt, fallbackNow = true } = params
  const normalizedStatus = String(status || '').toLowerCase()
  if (normalizedStatus === 'completed') {
    return String(remoteUpdatedAt || previousUpdatedAt || (fallbackNow ? new Date().toISOString() : '')).trim()
  }
  return String(previousUpdatedAt || remoteUpdatedAt || (fallbackNow ? new Date().toISOString() : '')).trim()
}

type SessionReviewEntry = ReviewHistoryItem & {
  file: File | null
  meta: ReviewMeta | null
  result: ReviewResultPayload | null
}

type HistoryApiItem = {
  run_id: string
  file_name?: string
  status: ReviewMeta['status']
  step?: string
  updated_at?: string
  document_ready?: boolean
}

function pickFilenameFromDisposition(contentDisposition: string | null, fallback: string) {
  if (!contentDisposition) return fallback
  const utf8 = contentDisposition.match(/filename\*=UTF-8''([^;]+)/i)
  if (utf8?.[1]) return decodeURIComponent(utf8[1])
  const plain = contentDisposition.match(/filename="?([^";]+)"?/i)
  return plain?.[1] || fallback
}

function createHistoryEntry(runId: string, file: File | null, meta?: ReviewMeta | null): SessionReviewEntry {
  const now = new Date().toISOString()
  return {
    id: runId,
    run_id: runId,
    file_name: file?.name || meta?.file_name,
    status: meta?.status || 'queued',
    summary: meta?.step || '准备审查',
    updated_at: now,
    created_at: now,
    available: true,
    file,
    meta: meta || null,
    result: null
  }
}

function upsertHistory(
  entries: SessionReviewEntry[],
  runId: string,
  updater: (prev: SessionReviewEntry) => SessionReviewEntry,
  fallbackFile: File | null,
  fallbackMeta?: ReviewMeta | null
) {
  const idx = entries.findIndex((item) => item.run_id === runId)
  if (idx >= 0) {
    const next = [...entries]
    next[idx] = updater(next[idx])
    return next.sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)))
  }
  const created = updater(createHistoryEntry(runId, fallbackFile, fallbackMeta))
  return [created, ...entries].sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)))
}

const NEW_RUN_ID_STORAGE_KEY = 'markup:newRunId'
const AUTO_AI_DISABLED_STORAGE_KEY = 'markup:autoAiApplyAllDisabled'
const AUTO_AI_TRIGGERED_STORAGE_KEY = 'markup:autoAiApplyAllTriggeredRunIds'
const ACTIVE_RUN_ID_STORAGE_KEY = 'markup:activeRunId'
const REVIEW_SNAPSHOT_STORAGE_KEY = 'markup:reviewSnapshotByRun'
const PREVIEW_WAITING_QUERY_KEY = 'preview_waiting'
const PREVIEW_AUTO_COMPLETE_QUERY_KEY = 'preview_auto_complete'

type PersistedReviewSnapshot = {
  runId: string
  meta: ReviewMeta
  result: ReviewResultPayload
  savedAt: string
}

function readAllReviewSnapshots(): Record<string, PersistedReviewSnapshot> {
  const raw = readSessionValue(REVIEW_SNAPSHOT_STORAGE_KEY)
  if (!raw) return {}
  try {
    const parsed = JSON.parse(raw) as Record<string, PersistedReviewSnapshot>
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function readReviewSnapshot(runId?: string | null): PersistedReviewSnapshot | null {
  const key = String(runId || '').trim()
  if (!key) return null
  const snapshots = readAllReviewSnapshots()
  const snapshot = snapshots[key]
  if (!snapshot) return null
  if (snapshot.runId !== key) return null
  if (snapshot.meta?.status !== 'completed') return null
  return snapshot.result ? snapshot : null
}

function writeReviewSnapshot(runId: string, meta: ReviewMeta, result: ReviewResultPayload) {
  const key = String(runId || '').trim()
  if (!key || meta.status !== 'completed') return
  const snapshots = readAllReviewSnapshots()
  snapshots[key] = {
    runId: key,
    meta,
    result,
    savedAt: new Date().toISOString()
  }
  writeSessionValue(REVIEW_SNAPSHOT_STORAGE_KEY, JSON.stringify(snapshots))
}

function navFromPathname(pathname: string): NavKey {
  const normalized = pathname !== '/' && pathname.endsWith('/') ? pathname.slice(0, -1) : pathname
  if (normalized === '/history') return 'history'
  if (normalized === '/review' || normalized.startsWith('/review/')) return 'result'
  return 'upload'
}

function pathForNav(key: NavKey) {
  if (key === 'history') return '/history'
  if (key === 'result' || key === 'waiting') return '/review'
  return '/upload'
}

function buildReviewPath(runId?: string | null) {
  if (!runId) return '/review'
  return `/review/${encodeURIComponent(runId)}`
}

function parseReviewRunId(pathname: string) {
  const normalized = pathname !== '/' && pathname.endsWith('/') ? pathname.slice(0, -1) : pathname
  if (!normalized || normalized === '/review') return null
  const prefix = '/review/'
  if (!normalized.startsWith(prefix)) return null
  const raw = normalized.slice(prefix.length).trim()
  if (!raw) return null
  try {
    return decodeURIComponent(raw)
  } catch {
    return raw
  }
}

function readSessionValue(key: string) {
  try {
    return window.sessionStorage.getItem(key)
  } catch {
    return null
  }
}

function writeSessionValue(key: string, value: string) {
  try {
    window.sessionStorage.setItem(key, value)
  } catch {
    return
  }
}

function parseTriggeredRunIds(raw: string | null) {
  if (!raw) return new Set<string>()
  try {
    const parsed = JSON.parse(raw) as unknown
    if (!Array.isArray(parsed)) return new Set<string>()
    return new Set(parsed.map((it) => String(it)))
  } catch {
    return new Set<string>()
  }
}

function readLocalValue(key: string) {
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

function writeLocalValue(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    return
  }
}

function removeLocalValue(key: string) {
  try {
    window.localStorage.removeItem(key)
  } catch {
    return
  }
}

function isPreviewWaitingMode() {
  try {
    const params = new URLSearchParams(window.location.search)
    const v = (params.get(PREVIEW_WAITING_QUERY_KEY) || '').toLowerCase()
    return v === '1' || v === 'true' || v === 'yes'
  } catch {
    return false
  }
}

function isPreviewAutoCompleteMode() {
  try {
    const params = new URLSearchParams(window.location.search)
    const v = (params.get(PREVIEW_AUTO_COMPLETE_QUERY_KEY) || '').toLowerCase()
    return v === '1' || v === 'true' || v === 'yes'
  } catch {
    return false
  }
}

function compactText(value: string) {
  return value.replace(/\s+/g, '')
}

const CLAUSE_UID_PATTERN = /^segment_[A-Za-z0-9_-]+::[A-Za-z0-9_.()（）-]+$/
const CLAUSE_REF_TOKEN_PATTERN = '[0-9一二三四五六七八九十百千万零〇]+(?:\\.[A-Za-z0-9]+)*'
const LEADING_CLAUSE_LABEL_PATTERNS = [
  new RegExp(`^\\s*(?:条款|条文|clause)\\s*${CLAUSE_REF_TOKEN_PATTERN}\\s*[:：，,]\\s*`, 'iu'),
  new RegExp(`^\\s*第?\\s*${CLAUSE_REF_TOKEN_PATTERN}\\s*(?:条|款)\\s*[:：，,]?\\s*`, 'u'),
  new RegExp(`^\\s*${CLAUSE_REF_TOKEN_PATTERN}\\s*[:：，,]\\s*`, 'u'),
  /^\s*[A-Za-z]+[0-9][A-Za-z0-9]*\s*[:：，,]\s*/u,
]
const CLAUSE_REF_SPLIT_RE = /\s*[、，,；;/]\s*/
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

function sanitizeAiTargetText(value: string) {
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

function isAggregateRiskLike(risk: any) {
  if (!risk || typeof risk !== 'object') return false
  const ai = risk.ai_rewrite && typeof risk.ai_rewrite === 'object'
    ? risk.ai_rewrite
    : risk.ai_apply && typeof risk.ai_apply === 'object'
      ? risk.ai_apply
      : {}
  return (
    Boolean(String(risk.aggregate_id || '').trim()) ||
    String(ai.workflow_kind || '').trim().toLowerCase() === 'aggregate' ||
    String(risk.risk_source_type || '').trim().toLowerCase() === 'anchored_multi_clause'
  )
}

function normalizePatchTargetForRisk(risk: any, value: string) {
  const raw = String(value || '').trim()
  if (!raw) return ''
  if (isAggregateRiskLike(risk)) return raw
  return sanitizeAiTargetText(raw)
}

function isUsablePatchTarget(value: string, preserveRaw = false) {
  const clean = preserveRaw ? String(value || '').trim() : sanitizeAiTargetText(value)
  if (!clean) return false
  if (CLAUSE_UID_PATTERN.test(clean)) return false
  const compact = compactText(clean)
  return compact.length >= 1
}

function pickBestPatchTarget(risk: any, preferredTarget?: string) {
  if (!risk || typeof risk !== 'object') return ''
  const preserveRaw = isAggregateRiskLike(risk)
  const locator = risk.locator && typeof risk.locator === 'object' ? risk.locator : {}
  const aiRewrite = risk.ai_rewrite && typeof risk.ai_rewrite === 'object' ? risk.ai_rewrite : {}
  const aiApply = risk.ai_apply && typeof risk.ai_apply === 'object' ? risk.ai_apply : {}
  // For AI accept/apply, always prioritize AI-provided target text.
  // Using evidence/anchor first can pick a shorter span and break replacement alignment.
  const buckets: string[][] = [
    [String(preferredTarget || '').trim(), String(aiRewrite.target_text || '').trim(), String(aiApply.target_text || '').trim()],
    [String(locator.matched_text || '').trim(), String(risk.target_text || '').trim()],
    [String(risk.anchor_text || '').trim()],
    [String(risk.evidence_text || '').trim()]
  ]

  const seen = new Set<string>()
  for (const candidates of buckets) {
    const usable = candidates
      .map((text) => normalizePatchTargetForRisk(risk, text).trim())
      .filter(Boolean)
      .filter((text) => {
        const key = compactText(text)
        if (!key || seen.has(key)) return false
        seen.add(key)
        return true
      })
      .filter((text) => isUsablePatchTarget(text, preserveRaw))
      .sort((a, b) => compactText(b).length - compactText(a).length)

    const strong = usable.find((text) => compactText(text).length >= 4)
    if (strong) return strong
    if (usable.length > 0) return usable[0]
  }
  return ''
}

function normalizeRiskTextForDisplay(value: unknown) {
  return String(value || '')
    .replace(/[【\[][^【】\[\]\n]{0,80}_[A-Za-z0-9-]{2,}[】\]]\s*/g, '')
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

function pickSuggestionInsertText(risk: any) {
  if (!risk || typeof risk !== 'object') return ''
  const candidates = [
    risk.suggestion,
    risk.suggestion_optimized,
    risk.suggestion_minimal,
    risk.basis
  ]
  for (const candidate of candidates) {
    const cleaned = normalizeRiskTextForDisplay(candidate)
    if (cleaned) return cleaned
  }
  return ''
}

function isAcceptedRiskStatus(status: unknown) {
  const normalized = String(status || '').trim().toLowerCase()
  return normalized === 'accepted' || normalized === 'ai_applied'
}

function isMissingClauseRisk(risk: any) {
  return String(risk?.risk_source_type || '').trim().toLowerCase() === 'missing_clause'
}

function getPrimaryClauseUidForRisk(risk: any) {
  if (!risk || typeof risk !== 'object') return ''
  const clauseUids = Array.isArray(risk?.clause_uids) ? risk.clause_uids : []
  const relatedClauseUids = Array.isArray(risk?.related_clause_uids) ? risk.related_clause_uids : []
  return String(clauseUids[0] || relatedClauseUids[0] || risk?.clause_uid || '').trim()
}

function getLocateClauseUidsForSuggestionInsert(risk: any) {
  const primaryUid = getPrimaryClauseUidForRisk(risk)
  if (isMissingClauseRisk(risk) && primaryUid) return [primaryUid]
  const allClauseUids =
    (Array.isArray(risk?.clause_uids) && risk.clause_uids.length > 0 ? risk.clause_uids : risk?.related_clause_uids) || []
  const refs = asClauseRefs(allClauseUids)
  return refs.length > 0 ? refs : primaryUid ? [primaryUid] : []
}

function asClauseRefs(value: unknown): string[] {
  const refs: string[] = []
  const seen = new Set<string>()
  const rawValues = Array.isArray(value) ? value : [value]
  for (const raw of rawValues) {
    const text = String(raw || '').trim()
    if (!text) continue
    const parts = text.split(CLAUSE_REF_SPLIT_RE).map((it) => it.trim()).filter(Boolean)
    for (const part of parts) {
      if (seen.has(part)) continue
      seen.add(part)
      refs.push(part)
    }
  }
  return refs
}

function AlertDialog(props: { open: boolean; title?: string; message: string; onClose: () => void }) {
  if (!props.open) return null
  return (
    <div className="editorOverlay" onClick={props.onClose}>
      <div className="editorSheet" onClick={(e) => e.stopPropagation()}>
        <div className="editorHeader">
          <div className="editorTitle">{props.title || '提示'}</div>
          <div className="editorActions">
            <button className="btnPrimarySolid" onClick={props.onClose}>
              我知道了
            </button>
          </div>
        </div>
        <div className="editorBody">
          <div className="editorReadonly">{props.message || '请求失败'}</div>
        </div>
      </div>
    </div>
  )
}

export default function App() {
  const navigate = useNavigate()
  const location = useLocation()
  const editorRef = useRef<DocumentEditorHandle | null>(null)
  const [activeNav, setActiveNav] = useState<NavKey>(() => navFromPathname(location.pathname))
  // Legacy UI had a collapsible sidebar. In the current product flow we do NOT show a left sidebar
  // on the review page (per Figma), so keep the flag only for backward compatibility.
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false)
  const prevNavRef = useRef<NavKey>('upload')
  const [file, setFile] = useState<File | null>(null)
  const [selectedReviewSide, setSelectedReviewSide] = useState<ReviewSideOption | null>(null)
  const [runId, setRunId] = useState<string | null>(null)
  const [meta, setMeta] = useState<ReviewMeta | null>(null)
  const [result, setResult] = useState<ReviewResultPayload | null>(null)
  const [isReviewing, setIsReviewing] = useState(false)
  const [routeHydratingRunId, setRouteHydratingRunId] = useState<string | null>(null)
  const [edits, setEdits] = useState<EditSummary[]>([])
  const [historyEntries, setHistoryEntries] = useState<SessionReviewEntry[]>([])
  const [serverConfig, setServerConfig] = useState<{ review_side: string; contract_type_hint: string } | null>(null)
  const [lastAcceptAllRiskIds, setLastAcceptAllRiskIds] = useState<string[]>([])
  const [docEditorReady, setDocEditorReady] = useState(false)
  const [dialog, setDialog] = useState<{ open: boolean; title: string; message: string }>({
    open: false,
    title: '提示',
    message: ''
  })
  const historyEntriesRef = useRef<SessionReviewEntry[]>([])
  const restoredAcceptedCommentRunRef = useRef<string | null>(null)

  // Some deployments only support the legacy AI endpoint (/ai_apply).
  // We auto-detect support for the newer AI rewrite endpoints once and cache the result
  // to avoid repeated 404s (and to prevent accidental re-triggering).
  const aiEndpointModeRef = useRef<'auto' | 'legacy' | 'new'>('auto')

  // Prevent duplicate history fetch in React StrictMode (dev) which mounts components twice.
  const historyFetchOnceRef = useRef(false)
  const restoreRunOnceRef = useRef(false)
  const pollingSeqRef = useRef(0)
  const currentRunIdRef = useRef<string | null>(null)
  const routeLoadingRunIdRef = useRef<string | null>(null)
  const newRunIdRef = useRef<string | null>(readSessionValue(NEW_RUN_ID_STORAGE_KEY))
  const autoAiTriggeredRef = useRef<Set<string>>(parseTriggeredRunIds(readSessionValue(AUTO_AI_TRIGGERED_STORAGE_KEY)))
  const autoAiInFlightRef = useRef<Set<string>>(new Set())
  const autoAiDisabledRef = useRef<boolean>(readSessionValue(AUTO_AI_DISABLED_STORAGE_KEY) === '1')
  const previewWaitingRef = useRef<boolean>(isPreviewWaitingMode())
  const previewAutoCompleteRef = useRef<boolean>(isPreviewAutoCompleteMode())
  const prevPathnameRef = useRef(location.pathname)
  const routeRunId = parseReviewRunId(location.pathname)
  const isRouteHydrating =
    !!routeRunId &&
    !previewWaitingRef.current &&
    routeHydratingRunId === routeRunId &&
    routeRunId !== runId

  const persistTriggeredRunIds = useCallback(() => {
    writeSessionValue(AUTO_AI_TRIGGERED_STORAGE_KEY, JSON.stringify(Array.from(autoAiTriggeredRef.current)))
  }, [])

  const persistCompletedReviewSnapshot = useCallback((targetRunId: string | null | undefined, nextMeta: ReviewMeta | null | undefined, nextResult: ReviewResultPayload | null | undefined) => {
    if (!targetRunId || !nextMeta || !nextResult) return
    if (String(nextMeta.status || '').toLowerCase() !== 'completed') return
    writeReviewSnapshot(targetRunId, nextMeta, nextResult)
  }, [])

  const openDialog = useCallback((message: string, title = '提示') => {
    setDialog({
      open: true,
      title,
      message: String(message || '请求失败')
    })
  }, [])

  const handleUploadFileChange = useCallback((nextFile: File | null) => {
    if (isReviewing) {
      openDialog('当前合同仍在审查中，请等待审查完成后再开始新的合同审查。')
      return
    }
    setFile(nextFile)
    setSelectedReviewSide(null)
  }, [isReviewing, openDialog])

  const handleReviewSideChange = useCallback((side: ReviewSideOption) => {
    if (isReviewing) {
      openDialog('当前合同仍在审查中，请等待审查完成后再切换审查立场。')
      return
    }
    setSelectedReviewSide(side)
  }, [isReviewing, openDialog])

  useEffect(() => {
    const originalAlert = window.alert.bind(window)
    window.alert = (message?: any) => {
      openDialog(String(message ?? ''))
    }
    return () => {
      window.alert = originalAlert
    }
  }, [openDialog])

  const maybeAutoApplyAllForRun = useCallback(
    async (params: {
      runId: string
      meta: ReviewMeta | null
      resultLoaded: boolean
      fallbackFile: File | null
    }) => {
      const { runId: targetRunId, meta: targetMeta, resultLoaded, fallbackFile } = params
      if (!resultLoaded) return
      if (!targetMeta || targetMeta.status !== 'completed') return
      if (autoAiDisabledRef.current) return
      if (targetRunId !== newRunIdRef.current) return
      if (autoAiTriggeredRef.current.has(targetRunId)) return
      if (autoAiInFlightRef.current.has(targetRunId)) return

      autoAiInFlightRef.current.add(targetRunId)

      try {
        const refreshResultOnce = async () => {
          const refreshedResp = await fetchNoStore(`/api/reviews/${targetRunId}/result`)
          if (!refreshedResp.ok) return false
          const refreshed = (await refreshedResp.json()) as ReviewResultPayload

          if (currentRunIdRef.current === targetRunId) {
            setResult(refreshed)
          }
          setHistoryEntries((entries) =>
            upsertHistory(
              entries,
              targetRunId,
              (prev) => ({
                ...prev,
                file: prev.file || fallbackFile,
                meta: targetMeta,
                result: refreshed,
                file_name: prev.file_name || fallbackFile?.name || refreshed.file_name,
                status: 'completed',
                summary: refreshed.risk_result_validated?.error_message || `已完成 · ${refreshed.risk_result_validated?.risk_result?.risk_items?.length || 0} 个风险点`,
                updated_at: new Date().toISOString(),
                available: true
              }),
              fallbackFile,
              targetMeta
            )
          )
          return true
        }

        let applyDone = false
        let applyResp: Response | null = null
        let applyErr: unknown = null
        const applyTask = (async () => {
          try {
            applyResp = await fetch(`/api/reviews/${targetRunId}/ai_apply_all`, {
              method: 'POST'
            })
          } catch (e) {
            applyErr = e
          } finally {
            applyDone = true
          }
        })()

        // While ai_apply_all is still running, poll result to progressively reflect completed rewrites.
        const startedAt = Date.now()
        while (!applyDone && Date.now() - startedAt < 180000) {
          await sleep(1200)
          if (targetMeta.status !== 'completed') break
          try {
            await refreshResultOnce()
          } catch {
            // ignore transient refresh failures during progressive polling
          }
        }

        await applyTask
        if (applyErr) {
          throw applyErr
        }
        if (!applyResp) {
          console.warn(`[auto ai_apply_all] no response for run ${targetRunId}`)
          return
        }
        const finalResp = applyResp as Response

        if (finalResp.status === 404) {
          autoAiDisabledRef.current = true
          writeSessionValue(AUTO_AI_DISABLED_STORAGE_KEY, '1')
          console.warn(`[auto ai_apply_all] endpoint not supported for run ${targetRunId}, disable for this session`)
          return
        }
        if (!finalResp.ok) {
          console.warn(`[auto ai_apply_all] failed for run ${targetRunId}: ${finalResp.status}`)
          return
        }

        // Mark as triggered ONLY after successful POST, so transient failures/aborts won't permanently lock this run.
        autoAiTriggeredRef.current.add(targetRunId)
        persistTriggeredRunIds()

        const refreshedOk = await refreshResultOnce()
        if (!refreshedOk) {
          console.warn(`[auto ai_apply_all] refresh result failed for run ${targetRunId}`)
          return
        }
      } catch (e: any) {
        console.warn(`[auto ai_apply_all] failed for run ${targetRunId}:`, e)
      } finally {
        autoAiInFlightRef.current.delete(targetRunId)
      }
    },
    [persistTriggeredRunIds]
  )

  // Remember the last non-result page so the review header "Back" button can return to it.
  useEffect(() => {
    if (activeNav !== 'result') prevNavRef.current = activeNav
  }, [activeNav])

  useEffect(() => {
    currentRunIdRef.current = runId
  }, [runId])

  useEffect(() => {
    setLastAcceptAllRiskIds([])
    restoredAcceptedCommentRunRef.current = null
    setDocEditorReady(false)
  }, [runId])

  useEffect(() => {
    if (activeNav !== 'result') {
      setDocEditorReady(false)
    }
  }, [activeNav])

  useEffect(() => {
    historyEntriesRef.current = historyEntries
  }, [historyEntries])

  useEffect(() => {
    persistCompletedReviewSnapshot(runId, meta, result)
  }, [runId, meta, result, persistCompletedReviewSnapshot])

  useEffect(() => {
    setActiveNav(navFromPathname(location.pathname))
  }, [location.pathname])


  useEffect(() => {
    if (location.pathname === '/') {
      navigate('/upload', { replace: true })
    }
  }, [location.pathname, navigate])

  useEffect(() => {
    const normalized = location.pathname !== '/' && location.pathname.endsWith('/') ? location.pathname.slice(0, -1) : location.pathname
    if (normalized !== '/review') return
    if (runId) {
      navigate(buildReviewPath(runId), { replace: true })
      return
    }
    navigate('/upload', { replace: true })
  }, [location.pathname, navigate, runId])

  useEffect(() => {
    if (previewWaitingRef.current) return
    if (routeRunId) return
    if (restoreRunOnceRef.current) return
    restoreRunOnceRef.current = true
    const savedRunId = readLocalValue(ACTIVE_RUN_ID_STORAGE_KEY)
    if (!savedRunId) return
    ;(async () => {
      try {
        const resp = await fetchNoStore(`/api/reviews/${savedRunId}`)
        if (!resp.ok) {
          removeLocalValue(ACTIVE_RUN_ID_STORAGE_KEY)
          return
        }
        const restoredMeta = (await resp.json()) as ReviewMeta
        if (restoredMeta.status === 'queued' || restoredMeta.status === 'running') {
          setRunId(savedRunId)
          setMeta(restoredMeta)
          setResult(null)
          setIsReviewing(true)
          navigate(buildReviewPath(savedRunId), { replace: true })
          return
        }
        removeLocalValue(ACTIVE_RUN_ID_STORAGE_KEY)
      } catch {
        removeLocalValue(ACTIVE_RUN_ID_STORAGE_KEY)
      }
    })()
  }, [navigate, routeRunId])

  useEffect(() => {
    if (!previewWaitingRef.current) return
    const previewRunId = 'preview_waiting'
    navigate(buildReviewPath(previewRunId), { replace: true })
    setRunId(previewRunId)
    setResult(null)
    setIsReviewing(true)
    setMeta({
      run_id: previewRunId,
      status: 'running',
      step: '正在解析合同结构…',
      progress: 36,
      file_name: '示例合同.docx'
    })
  }, [handleUploadFileChange, navigate])

  useEffect(() => {
    if (!previewWaitingRef.current || !previewAutoCompleteRef.current) return
    const timer = window.setTimeout(() => {
      const previewRunId = 'preview_waiting'
      setMeta({
        run_id: previewRunId,
        status: 'completed',
        step: '已完成'
      })
      setIsReviewing(false)
      setResult({
        run_id: previewRunId,
        status: 'completed',
        file_name: '示例合同.docx',
        review_side: 'supplier',
        contract_type_hint: 'service_agreement',
        merged_clauses: [],
        risk_result_validated: {
          is_valid: true,
          risk_result: {
            risk_items: [
              {
                risk_id: 1,
                dimension: '违约责任',
                risk_label: '违约金比例约定不明确',
                risk_level: 'medium',
                issue: '违约责任条款缺少明确违约金比例，执行时可能产生争议。',
                basis: '合同应明确违约责任与计算方式，避免履约争议与举证困难。',
                suggestion: '建议补充“违约金按未履行部分金额的5%计算”并明确支付时限。',
                status: 'pending',
                ai_rewrite: {
                  state: 'succeeded',
                  target_text: '违约责任条款',
                  revised_text: '违约责任条款（含明确违约金比例与支付时限）',
                  comment_text: '建议在违约责任条款中补充计算口径与履行时限。',
                  created_at: new Date().toISOString()
                },
                ai_rewrite_decision: 'proposed'
              }
            ]
          }
        },
        download_ready: false,
        download_url: null
      })
    }, 2600)
    return () => window.clearTimeout(timer)
  }, [])

  const statusText = useMemo(() => {
    if (!meta) return ''
    if (meta.status === 'failed') return meta.error || '任务失败'
    if (meta.status === 'completed') return meta.warning ? `完成（${meta.warning}）` : '完成'
    return meta.step || meta.status
  }, [meta])

  const clauseTextByUid = useMemo(() => {
    const map: Record<string, string> = {}
    const clauses = result?.merged_clauses || []
    for (const clause of clauses) {
      if (!clause.clause_uid) continue
      map[clause.clause_uid] = clause.clause_text || ''
    }
    return map
  }, [result])

  const riskCount = result?.risk_result_validated?.risk_result?.risk_items?.length || 0
  const pendingRiskCount = useMemo(() => {
    const items = result?.risk_result_validated?.risk_result?.risk_items || []
    return items.filter((r) => {
      const status = String(r?.status || 'pending').trim().toLowerCase()
      return status === '' || status === 'pending'
    }).length
  }, [result])
  const acceptedRiskIds = useMemo(() => {
    const items = result?.risk_result_validated?.risk_result?.risk_items || []
    return items
      .filter((r) => isAcceptedRiskStatus(r?.status))
      .map((r) => String(r.risk_id))
      .filter(Boolean)
  }, [result])
  const riskStats = useMemo(() => {
    const items = result?.risk_result_validated?.risk_result?.risk_items || []
    const next = { total: items.length, high: 0, medium: 0, low: 0 }
    for (const r of items) {
      if (r.risk_level === 'high') next.high += 1
      else if (r.risk_level === 'medium') next.medium += 1
      else if (r.risk_level === 'low') next.low += 1
    }
    return next
  }, [result])

  const historyStats = useMemo(() => {
    let completed = 0
    let running = 0
    let failed = 0
    for (const item of historyEntries) {
      if (item.status === 'completed') completed += 1
      if (item.status === 'running' || item.status === 'queued') running += 1
      if (item.status === 'failed') failed += 1
    }
    return {
      total: historyEntries.length,
      completed,
      running,
      failed
    }
  }, [historyEntries])

  const refreshHistoryFromApi = useCallback(async () => {
    try {
      const resp = await fetchNoStore('/api/reviews/history?limit=30')
      if (!resp.ok) return
      const data = (await resp.json()) as { items?: HistoryApiItem[] }
    const remoteItems = data.items || []
    setHistoryEntries((entries) => {
      const byRunId = new Map(entries.map((it) => [it.run_id, it]))
      for (const item of remoteItems) {
        const prev = byRunId.get(item.run_id)
        byRunId.set(item.run_id, {
          id: prev?.id || item.run_id,
          run_id: item.run_id,
          file_name: prev?.file_name || item.file_name,
          status: item.status || prev?.status || 'queued',
          summary: prev?.summary || item.step || item.status,
          updated_at: resolveHistoryUpdatedAt({
            status: item.status || prev?.status,
            remoteUpdatedAt: item.updated_at,
            previousUpdatedAt: prev?.updated_at
          }),
          created_at: prev?.created_at || item.updated_at || new Date().toISOString(),
          available: item.document_ready ?? prev?.available ?? true,
          file: prev?.file ?? null,
          meta: prev?.meta ?? null,
          result: prev?.result ?? null
        })
      }
      return Array.from(byRunId.values()).sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)))
    })
    } catch {
      // Backend may be down during local development; do not spam the console.
      return
    }
  }, [])

  const loadReviewWorkspace = useCallback(async (params: {
    runId: string
    file: File | null
    meta: ReviewMeta | null
    result: ReviewResultPayload | null
    fileName?: string | null
  }) => {
    const { runId: targetRunId, file: seedFile, meta: seedMeta, result: seedResult, fileName } = params

    let nextMeta = seedMeta
    let nextFile = seedFile
    let nextResult = seedResult

    const statusResp = await fetchNoStore(`/api/reviews/${targetRunId}`)
    if (!statusResp.ok) {
      const text = await statusResp.text()
      throw new Error(text || `获取审查状态失败（${statusResp.status}）`)
    }
    nextMeta = (await statusResp.json()) as ReviewMeta

    const effectiveStatus = String(nextMeta?.status || '').toLowerCase()

    if (!nextFile) {
      const docResp = await fetchNoStore(`/api/reviews/${targetRunId}/document`)
      if (docResp.ok) {
        const blob = await docResp.blob()
        const fallbackName = nextMeta?.file_name || fileName || `${targetRunId}.docx`
        const resolvedFileName = pickFilenameFromDisposition(docResp.headers.get('content-disposition'), fallbackName)
        nextFile = new File([blob], resolvedFileName, {
          type: blob.type || 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        })
      }
    }

    if (effectiveStatus === 'completed') {
      const resultResp = await fetchNoStore(`/api/reviews/${targetRunId}/result`)
      if (!resultResp.ok) {
        const text = await resultResp.text()
        throw new Error(text || `获取审查结果失败（${resultResp.status}）`)
      }
      nextResult = (await resultResp.json()) as ReviewResultPayload
    }

    return {
      nextMeta,
      nextFile,
      nextResult,
      effectiveStatus
    }
  }, [])

  const applyLoadedReviewWorkspace = useCallback((params: {
    runId: string
    file: File | null
    meta: ReviewMeta | null
    result: ReviewResultPayload | null
    effectiveStatus: string
    fallbackFileName?: string | null
  }) => {
    const { runId: targetRunId, file: nextFile, meta: nextMeta, result: nextResult, effectiveStatus, fallbackFileName } = params

    setFile(nextFile)
    setRunId(targetRunId)
    setMeta(nextMeta)

    if (effectiveStatus === 'queued' || effectiveStatus === 'running') {
      setResult(null)
      setIsReviewing(true)
      writeLocalValue(ACTIVE_RUN_ID_STORAGE_KEY, targetRunId)
    } else {
      setResult(nextResult)
      setIsReviewing(false)
      removeLocalValue(ACTIVE_RUN_ID_STORAGE_KEY)
      if (nextMeta && nextResult) {
        persistCompletedReviewSnapshot(targetRunId, nextMeta, nextResult)
      }
    }

    setHistoryEntries((entries) =>
      upsertHistory(
        entries,
        targetRunId,
        (prev) => ({
          ...prev,
          file: nextFile,
          meta: nextMeta,
          result: effectiveStatus === 'completed' ? nextResult : null,
          file_name: prev.file_name || nextFile?.name || nextMeta?.file_name || fallbackFileName || targetRunId,
          status: (nextMeta?.status || prev.status) as ReviewMeta['status'],
          summary:
            nextMeta?.error ||
            nextMeta?.warning ||
            nextMeta?.step ||
            prev.summary ||
            prev.status,
          updated_at: resolveHistoryUpdatedAt({
            status: nextMeta?.status || prev.status,
            remoteUpdatedAt: nextMeta?.updated_at,
            previousUpdatedAt: prev.updated_at
          }),
          available: true
        }),
        nextFile,
        nextMeta
      )
    )
  }, [persistCompletedReviewSnapshot])

  const openSessionReview = useCallback(async (item: ReviewHistoryItem) => {
    if (item.available === false) {
      alert('该审查记录缺少原始合同文件（后端返回 document_ready=false），无法打开。')
      return
    }

    setEdits([])

    const { nextMeta, nextFile, nextResult, effectiveStatus } = await loadReviewWorkspace({
      runId: item.run_id,
      file: item.file ?? null,
      meta: item.meta ?? null,
      result: item.result ?? null,
      fileName: item.file_name
    })

    navigate(buildReviewPath(item.run_id))
    applyLoadedReviewWorkspace({
      runId: item.run_id,
      file: nextFile,
      meta: nextMeta,
      result: nextResult,
      effectiveStatus,
      fallbackFileName: item.file_name
    })

    await maybeAutoApplyAllForRun({
      runId: item.run_id,
      meta: nextMeta,
      resultLoaded: effectiveStatus === 'completed' && nextResult != null,
      fallbackFile: nextFile
    })

  }, [applyLoadedReviewWorkspace, loadReviewWorkspace, maybeAutoApplyAllForRun, navigate])

  const startReview = useCallback(async () => {
    if (isReviewing) {
      openDialog('当前合同仍在审查中，请等待审查完成后再开始新的合同审查。')
      return
    }
    if (!file || !selectedReviewSide) return
    setIsReviewing(true)
    setResult(null)
    setMeta(null)
    setRunId(null)
    setEdits([])

    const form = new FormData()
    form.append('file', file)
    form.append('review_side', selectedReviewSide)
    form.append('contract_type_hint', serverConfig?.contract_type_hint ?? 'service_agreement')

    const resp = await fetch('/api/reviews', { method: 'POST', body: form })
    if (!resp.ok) {
      const text = await resp.text()
      throw new Error(text)
    }
    const data = (await resp.json()) as { run_id: string }
    newRunIdRef.current = data.run_id
    writeSessionValue(NEW_RUN_ID_STORAGE_KEY, data.run_id)
    writeLocalValue(ACTIVE_RUN_ID_STORAGE_KEY, data.run_id)
    const nextMeta: ReviewMeta = {
      run_id: data.run_id,
      status: 'queued',
      file_name: file.name,
      step: '已上传，等待开始审查'
    }
    setRunId(data.run_id)
    setMeta(nextMeta)
    setHistoryEntries((entries) =>
      upsertHistory(
        entries,
        data.run_id,
        (prev) => ({
          ...prev,
          file,
          meta: nextMeta,
          result: null,
          file_name: file.name,
          status: 'queued',
          summary: nextMeta.step || '已上传，等待开始审查',
          updated_at: new Date().toISOString(),
          available: true
        }),
        file,
        nextMeta
      )
    )
    navigate(buildReviewPath(data.run_id))
  }, [file, navigate, selectedReviewSide, serverConfig])

  useEffect(() => {
    let cancelled = false
    const abortController = new AbortController()
    pollingSeqRef.current += 1
    const seq = pollingSeqRef.current
    // Poll only when review workspace is visible and result has not been produced yet.
    // Do NOT depend on `meta` here, otherwise every status update restarts and aborts the in-flight completed chain.
    const shouldPoll = activeNav === 'result' && !result && !runId?.startsWith('preview_')
    if (!runId || !shouldPoll) return

    ;(async () => {
      try {
        while (!cancelled && pollingSeqRef.current === seq) {
          const resp = await fetchNoStore(`/api/reviews/${runId}`, { signal: abortController.signal })
          const m = (await resp.json()) as ReviewMeta
          if (cancelled) return
          setMeta(m)
          setIsReviewing(m.status === 'queued' || m.status === 'running')
          setHistoryEntries((entries) =>
            upsertHistory(
              entries,
              runId,
              (prev) => ({
              ...prev,
              file: prev.file || file,
              meta: m,
              file_name: prev.file_name || file?.name || m.file_name,
              status: m.status,
              summary: m.error || m.warning || m.step || m.status,
              updated_at: resolveHistoryUpdatedAt({
                status: m.status,
                remoteUpdatedAt: m.updated_at,
                previousUpdatedAt: prev.updated_at
              }),
              available: true
            }),
            file,
              m
            )
          )

          if (m.status === 'completed') {
            const [docResp, resultResp] = await Promise.all([
              fetchNoStore(`/api/reviews/${runId}/document`, { signal: abortController.signal }),
              fetchNoStore(`/api/reviews/${runId}/result`, { signal: abortController.signal })
            ])
            if (!resultResp.ok) {
              const text = await resultResp.text()
              throw new Error(text || `获取结果失败（${resultResp.status}）`)
            }
            const payload = (await resultResp.json()) as ReviewResultPayload
            let nextFile = file
            if (docResp.ok) {
              const blob = await docResp.blob()
              const fallbackName = m.file_name || file?.name || `${runId}.docx`
              const fileName = pickFilenameFromDisposition(docResp.headers.get('content-disposition'), fallbackName)
              nextFile = new File([blob], fileName, { type: blob.type || 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' })
            }
            if (cancelled) return
            handleUploadFileChange(nextFile)
            setResult(payload)
            setIsReviewing(false)
            navigate(buildReviewPath(runId), { replace: true })
            removeLocalValue(ACTIVE_RUN_ID_STORAGE_KEY)
            setHistoryEntries((entries) =>
              upsertHistory(
                entries,
                runId,
                (prev) => ({
                  ...prev,
                  file: prev.file || nextFile,
                  meta: m,
                  result: payload,
                  file_name: prev.file_name || nextFile?.name || payload.file_name,
                  status: 'completed',
                  summary: payload.risk_result_validated?.error_message || `已完成 · ${payload.risk_result_validated?.risk_result?.risk_items?.length || 0} 个风险点`,
                  updated_at: resolveHistoryUpdatedAt({
                    status: 'completed',
                    remoteUpdatedAt: m.updated_at,
                    previousUpdatedAt: prev.updated_at
                  }),
                  available: true
                }),
                nextFile,
                m
              )
            )
            await maybeAutoApplyAllForRun({
              runId,
              meta: m,
              resultLoaded: true,
              fallbackFile: nextFile
            })
            void refreshHistoryFromApi()
            break
          }
          if (m.status === 'failed') {
            setIsReviewing(false)
            removeLocalValue(ACTIVE_RUN_ID_STORAGE_KEY)
            void refreshHistoryFromApi()
            break
          }
          await sleep(1800)
        }
      } catch (e) {
        if ((e as any)?.name === 'AbortError') return
        if (!cancelled) {
          setIsReviewing(false)
          const failedMeta = { run_id: runId, status: 'failed', error: String(e) } as ReviewMeta
          setMeta(failedMeta)
          removeLocalValue(ACTIVE_RUN_ID_STORAGE_KEY)
          setHistoryEntries((entries) =>
            upsertHistory(
              entries,
              runId,
              (prev) => ({
                ...prev,
                file: prev.file || file,
                meta: failedMeta,
                status: 'failed',
                summary: String(e),
                updated_at: resolveHistoryUpdatedAt({
                  status: 'failed',
                  remoteUpdatedAt: failedMeta.updated_at,
                  previousUpdatedAt: prev.updated_at
                }),
                available: true
              }),
              file,
              failedMeta
            )
          )
        }
      }
    })()

    return () => {
      cancelled = true
      abortController.abort()
    }
  }, [runId, file, activeNav, result, maybeAutoApplyAllForRun, refreshHistoryFromApi, navigate, selectedReviewSide])

  useEffect(() => {
    if (historyFetchOnceRef.current) return
    historyFetchOnceRef.current = true
    void refreshHistoryFromApi()
  }, [refreshHistoryFromApi])

  useEffect(() => {
    void (async () => {
      try {
        const resp = await fetch('/api/config')
        if (resp.ok) {
          const config = (await resp.json()) as { review_side: string; contract_type_hint: string }
          setServerConfig(config)
        }
      } catch {
        // ignore config fetch errors, use backend defaults
      }
    })()
  }, [])

  useEffect(() => {
    if (previewWaitingRef.current) return
    if (!routeRunId) return
    if (routeRunId === runId) return
    if (routeLoadingRunIdRef.current === routeRunId) return

    routeLoadingRunIdRef.current = routeRunId
    setRouteHydratingRunId(routeRunId)
    let cancelled = false

    ;(async () => {
      try {
        const cached = historyEntriesRef.current.find((it) => it.run_id === routeRunId) || null
        const snapshot = readReviewSnapshot(routeRunId)
        const { nextMeta, nextFile, nextResult, effectiveStatus } = await loadReviewWorkspace({
          runId: routeRunId,
          file: cached?.file ?? null,
          meta: cached?.meta ?? snapshot?.meta ?? null,
          result: cached?.result ?? snapshot?.result ?? null,
          fileName: cached?.file_name || snapshot?.meta?.file_name || null
        })

        if (cancelled) return

        applyLoadedReviewWorkspace({
          runId: routeRunId,
          file: nextFile,
          meta: nextMeta,
          result: nextResult,
          effectiveStatus,
          fallbackFileName: cached?.file_name || snapshot?.meta?.file_name || routeRunId
        })

        await maybeAutoApplyAllForRun({
          runId: routeRunId,
          meta: nextMeta,
          resultLoaded: effectiveStatus === 'completed' && nextResult != null,
          fallbackFile: nextFile
        })
      } catch (e) {
        if (cancelled) return
        console.warn(`[review-route] failed to load run ${routeRunId}:`, e)
        navigate('/history', { replace: true })
        alert(`无法打开审查记录 ${routeRunId}：${String(e)}`)
      } finally {
        if (!cancelled) {
          setRouteHydratingRunId((current) => (current === routeRunId ? null : current))
        }
        if (routeLoadingRunIdRef.current === routeRunId) {
          routeLoadingRunIdRef.current = null
        }
      }
    })()

    return () => {
      cancelled = true
      if (routeLoadingRunIdRef.current === routeRunId) {
        routeLoadingRunIdRef.current = null
      }
      setRouteHydratingRunId((current) => (current === routeRunId ? null : current))
    }
  }, [routeRunId, runId, navigate, loadReviewWorkspace, applyLoadedReviewWorkspace, maybeAutoApplyAllForRun])

  useEffect(() => {
    if (!runId || !result || !meta || meta.status !== 'completed') return

    let cancelled = false
    const refreshCompletedResult = async () => {
      try {
        const [statusResp, resultResp] = await Promise.all([
          fetchNoStore(`/api/reviews/${runId}`),
          fetchNoStore(`/api/reviews/${runId}/result`)
        ])
        if (!statusResp.ok || !resultResp.ok || cancelled) return

        const nextMeta = (await statusResp.json()) as ReviewMeta
        const nextResult = (await resultResp.json()) as ReviewResultPayload
        if (cancelled) return

        setMeta(nextMeta)
        setResult(nextResult)
        setHistoryEntries((entries) =>
          upsertHistory(
            entries,
            runId,
            (prev) => ({
              ...prev,
              meta: nextMeta,
              result: nextResult,
              status: nextMeta.status,
              summary: nextMeta.error || nextMeta.warning || nextMeta.step || prev.summary || prev.status,
              updated_at: resolveHistoryUpdatedAt({
                status: nextMeta.status,
                remoteUpdatedAt: nextMeta.updated_at,
                previousUpdatedAt: prev.updated_at
              }),
              available: true
            }),
            file,
            nextMeta
          )
        )
      } catch {
        return
      }
    }

    const onFocus = () => {
      void refreshCompletedResult()
    }
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        void refreshCompletedResult()
      }
    }

    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      cancelled = true
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [runId, result, meta, file])

  const onLocateRisk = useCallback((opts: { riskId?: number | string; riskSourceType?: string; targetText?: string; anchorText?: string; evidenceText?: string; clauseUids?: string[] }) => {
    const { riskId, riskSourceType, ...locateOpts } = opts
    const items = (result?.risk_result_validated?.risk_result?.risk_items || []) as any[]
    const riskForLocate = riskId !== undefined && riskId !== null
      ? items.find((it) => String(it?.risk_id) === String(riskId))
      : undefined
    editorRef.current?.locateRisk({
      ...locateOpts,
      riskSourceType: String(riskSourceType || riskForLocate?.risk_source_type || ''),
      targetText: normalizePatchTargetForRisk(riskForLocate, String(locateOpts.targetText || '')),
      anchorText: sanitizeAiTargetText(String(locateOpts.anchorText || '')),
      evidenceText: sanitizeAiTargetText(String(locateOpts.evidenceText || ''))
    })
  }, [result])

  /**
   * Update a single risk item in both `result` and `historyEntries`.
   * This is used by the new AI rewrite flow (ai_accept/ai_edit/ai_reject)
   * to avoid duplicating state update code and reduce regression risk.
   */
  const mergeUpdatedRisk = useCallback(
    (riskId: number | string, updated: any) => {
      setResult((prev) => {
        if (!prev) return prev
        const nextItems = (prev.risk_result_validated?.risk_result?.risk_items || []).map((it) =>
          String(it.risk_id) === String(riskId) ? { ...it, ...updated } : it
        )
        return {
          ...prev,
          risk_result_validated: {
            ...prev.risk_result_validated,
            risk_result: {
              ...prev.risk_result_validated.risk_result,
              risk_items: nextItems
            }
          }
        }
      })

      setHistoryEntries((entries) =>
        entries.map((entry) => {
          if (entry.run_id !== runId || !entry.result) return entry
          const nextItems = (entry.result.risk_result_validated?.risk_result?.risk_items || []).map((it) =>
            String(it.risk_id) === String(riskId) ? { ...it, ...updated } : it
          )
          return {
            ...entry,
            result: {
              ...entry.result,
              risk_result_validated: {
                ...entry.result.risk_result_validated,
                risk_result: {
                  ...entry.result.risk_result_validated.risk_result,
                  risk_items: nextItems
                }
              }
            },
            updated_at: resolveHistoryUpdatedAt({
              status: entry.status,
              remoteUpdatedAt: entry.meta?.updated_at,
              previousUpdatedAt: entry.updated_at
            })
          }
        })
      )
    },
    [runId, result]
  )

  const onRejectRisk = useCallback(
    async (riskId: number | string) => {
      if (!runId) throw new Error('当前没有可操作的 run_id')
      const isPreview = String(runId).startsWith('preview_')

      if (!isPreview) {
        const resp = await fetch(`/api/reviews/${runId}/risks/${encodeURIComponent(String(riskId))}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: 'rejected' })
        })
        if (!resp.ok) {
          throw new Error(await readErrorDetail(resp))
        }
      }
      mergeUpdatedRisk(riskId, { status: 'rejected', ai_rewrite_decision: 'rejected' })
      editorRef.current?.removeSuggestionInsertComment(riskId)
    },
    [runId, mergeUpdatedRisk]
  )

  const onSetRiskStatus = useCallback(
    async (riskId: number | string, status: 'pending' | 'accepted' | 'rejected') => {
      if (!runId) throw new Error('当前没有可操作的 run_id')
      let handledByPayload = false
      if (!String(runId).startsWith('preview_')) {
        const resp = await fetch(`/api/reviews/${runId}/risks/${encodeURIComponent(String(riskId))}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status })
        })
        if (!resp.ok) {
          throw new Error(await readErrorDetail(resp))
        }
        const payload = (await resp.json()) as { item?: any }
        if (payload.item) {
          mergeUpdatedRisk(riskId, payload.item)
          handledByPayload = true
        }
      }
      if (!handledByPayload) {
        mergeUpdatedRisk(
          riskId,
          status === 'pending' ? { status, ai_rewrite_decision: 'proposed' } : { status }
        )
      }
      if (status === 'pending') {
        editorRef.current?.revertAiPatch(riskId)
        editorRef.current?.removeSuggestionInsertComment(riskId)
      }
    },
    [runId, mergeUpdatedRisk]
  )

  const onAcceptRisk = useCallback(
    async (riskId: number | string, opts?: { revisedText?: string }) => {
      if (!runId) throw new Error('当前没有可操作的 run_id')
      const isPreview = String(runId).startsWith('preview_')
      const items = (result?.risk_result_validated?.risk_result?.risk_items || []) as any[]
      const found = items.find((it) => String(it?.risk_id) === String(riskId))
      const ai = (found?.ai_rewrite || found?.ai_apply || null) as any
      const targetText = pickBestPatchTarget(found, String(ai?.target_text || ''))
      const revisedText = String(opts?.revisedText || ai?.revised_text || '').trim()
      const suggestionInsertText = normalizeRiskTextForDisplay(opts?.revisedText || pickSuggestionInsertText(found))
      const aiState = String(ai?.state || '').toLowerCase()
      const shouldApplyAi = Boolean(ai && (aiState === 'succeeded' || revisedText))
      const preserveRawTarget = isAggregateRiskLike(found)
      let appliedLocally = false
      let acceptedTargetText = targetText
      let acceptedRevisedText = revisedText

      if (shouldApplyAi && !targetText) {
        throw new Error('未能在文档中定位到可替换文本，接受已取消。请先点击“定位原文”确认后再试。')
      }

      if (shouldApplyAi && targetText) {
        const applied =
          editorRef.current?.applyAiPatch({
            patchId: riskId,
            targetText,
            revisedText,
            preserveRawTarget
          }) || false
        if (!applied) {
          throw new Error('未能在文档中定位到可替换文本，接受已取消。请先点击“定位原文”确认后再试。')
        }
        appliedLocally = true
        const appliedPatch = editorRef.current?.getAppliedAiPatch(riskId)
        if (appliedPatch?.targetText) acceptedTargetText = appliedPatch.targetText
        if (appliedPatch) acceptedRevisedText = appliedPatch.revisedText
      }

      try {
        let acceptedByAiEndpoint = false
        if (shouldApplyAi) {
          if (!isPreview) {
            const resp = await fetch(`/api/reviews/${runId}/risks/${encodeURIComponent(String(riskId))}/ai_accept`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ revised_text: acceptedRevisedText, target_text: acceptedTargetText || undefined })
            })
            if (!resp.ok) {
              throw new Error(await readErrorDetail(resp))
            }
            const payload = (await resp.json()) as { item?: any }
            if (payload.item) mergeUpdatedRisk(riskId, payload.item)
            acceptedByAiEndpoint = true
          } else {
            mergeUpdatedRisk(riskId, { ai_rewrite_decision: 'accepted' })
          }
        }
        if (!acceptedByAiEndpoint) {
          await onSetRiskStatus(riskId, 'accepted')
          if (!shouldApplyAi && suggestionInsertText) {
            editorRef.current?.addSuggestionInsertComment({
              riskId,
              suggestionText: suggestionInsertText,
              targetText,
              anchorText: String(found?.anchor_text || ''),
              evidenceText: String(found?.evidence_text || ''),
              clauseUids: getLocateClauseUidsForSuggestionInsert(found)
            })
          }
        }
      } catch (error) {
        if (appliedLocally) {
          editorRef.current?.revertAiPatch(riskId)
        }
        throw error
      }
    },
    [onSetRiskStatus, runId, result, mergeUpdatedRisk]
  )

  const onAcceptAllRisks = useCallback(async () => {
    if (!runId) throw new Error('当前没有可操作的 run_id')
    const isPreview = String(runId).startsWith('preview_')
    const currentItems = (result?.risk_result_validated?.risk_result?.risk_items || []) as any[]
    const acceptedRiskIds: string[] = []
    const failedRiskIds: string[] = []

    for (const item of currentItems) {
      const riskId = item?.risk_id
      if (riskId === undefined || riskId === null) continue
      const status = String(item?.status || 'pending').toLowerCase()
      if (isAcceptedRiskStatus(status) || status === 'rejected') continue
      const ai = (item?.ai_rewrite || item?.ai_apply || null) as any
      const aiState = String(ai?.state || '').toLowerCase()
      const targetText = pickBestPatchTarget(item, String(ai?.target_text || ''))
      const revisedText = String(ai?.revised_text || '').trim()
      const suggestionInsertText = pickSuggestionInsertText(item)
      const shouldApplyAi = Boolean(ai && (aiState === 'succeeded' || revisedText))
      const preserveRawTarget = isAggregateRiskLike(item)
      let appliedLocally = false
      let acceptedTargetText = targetText
      let acceptedRevisedText = revisedText

      try {
        let acceptedByAiEndpoint = false
        if (shouldApplyAi && !targetText) {
          throw new Error('未能在文档中定位到可替换文本')
        }
        if (shouldApplyAi && targetText) {
          const applied =
            editorRef.current?.applyAiPatch({
              patchId: riskId,
              targetText,
              revisedText,
              preserveRawTarget
            }) || false
          if (!applied) {
            throw new Error('未能在文档中定位到可替换文本')
          }
          appliedLocally = true
          const appliedPatch = editorRef.current?.getAppliedAiPatch(riskId)
          if (appliedPatch?.targetText) acceptedTargetText = appliedPatch.targetText
          if (appliedPatch) acceptedRevisedText = appliedPatch.revisedText
        }
        if (shouldApplyAi) {
          if (!isPreview) {
            const resp = await fetch(`/api/reviews/${runId}/risks/${encodeURIComponent(String(riskId))}/ai_accept`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ revised_text: acceptedRevisedText, target_text: acceptedTargetText || undefined })
            })
            if (!resp.ok) {
              throw new Error(await readErrorDetail(resp))
            }
            const payload = (await resp.json()) as { item?: any }
            if (payload.item) mergeUpdatedRisk(riskId, payload.item)
            acceptedByAiEndpoint = true
          } else {
            mergeUpdatedRisk(riskId, { ai_rewrite_decision: 'accepted' })
          }
        }
        if (!acceptedByAiEndpoint) {
          await onSetRiskStatus(riskId, 'accepted')
          if (!shouldApplyAi && suggestionInsertText) {
            editorRef.current?.addSuggestionInsertComment({
              riskId,
              suggestionText: suggestionInsertText,
              targetText,
              anchorText: String(item?.anchor_text || ''),
              evidenceText: String(item?.evidence_text || ''),
              clauseUids: getLocateClauseUidsForSuggestionInsert(item)
            })
          }
        }
        acceptedRiskIds.push(String(riskId))
      } catch (error) {
        if (appliedLocally) {
          editorRef.current?.revertAiPatch(riskId)
        }
        failedRiskIds.push(String(riskId))
        console.warn('accept all risk failed', riskId, error)
      }
    }

    if (failedRiskIds.length > 0) {
      if (acceptedRiskIds.length > 0) {
        // Even if some items fail, allow "一键撤销接受全部" to rollback succeeded ones.
        setLastAcceptAllRiskIds(acceptedRiskIds)
      }
      throw new Error(`以下风险未接受成功：${failedRiskIds.join('、')}`)
    }
    setLastAcceptAllRiskIds(acceptedRiskIds)
  }, [runId, result, onSetRiskStatus, mergeUpdatedRisk])

  /**
   * New AI rewrite flow (latest backend).
   * We attempt the new endpoints first; if the backend hasn't been updated,
   * we fall back to the legacy ai_apply / status patch behavior.
   */
  const onAiAcceptRisk = useCallback(
    async (riskId: number | string, revisedText?: string) => {
      await onAcceptRisk(riskId, revisedText ? { revisedText } : undefined)
    },
    [onAcceptRisk]
  )

  const onUndoAcceptAllRisks = useCallback(async () => {
    const targetRiskIds = lastAcceptAllRiskIds.length > 0 ? lastAcceptAllRiskIds : acceptedRiskIds
    if (targetRiskIds.length === 0) return
    const failed: string[] = []
    for (const riskId of targetRiskIds) {
      try {
        await onSetRiskStatus(riskId, 'pending')
      } catch {
        failed.push(riskId)
      }
    }
    if (failed.length > 0) {
      setLastAcceptAllRiskIds(failed)
      throw new Error(`以下风险撤销失败：${failed.join('、')}`)
    }
    setLastAcceptAllRiskIds([])
  }, [lastAcceptAllRiskIds, acceptedRiskIds, onSetRiskStatus])

  useEffect(() => {
    if (!runId || !file || !result || !docEditorReady) return
    const items = (result.risk_result_validated?.risk_result?.risk_items || []) as any[]
    const acceptedItems = items.filter((item) => item && isAcceptedRiskStatus(item.status))
    if (acceptedItems.length === 0) {
      restoredAcceptedCommentRunRef.current = null
      return
    }

    const restoreKey = `${runId}:${acceptedItems
      .map((item) => `${item.risk_id}:${String(item.status || '')}:${String(item.ai_rewrite_decision || '')}`)
      .join('|')}`
    if (restoredAcceptedCommentRunRef.current === restoreKey) return

    const editor = editorRef.current
    if (!editor) return

    let restoredCount = 0
    for (const item of acceptedItems) {
      const riskId = item?.risk_id
      if (riskId === undefined || riskId === null) continue

      const ai = (item?.ai_rewrite || item?.ai_apply || null) as any
      const aiState = String(ai?.state || '').toLowerCase()
      const targetText = pickBestPatchTarget(item, String(ai?.target_text || ''))
      const revisedText = String(ai?.revised_text || '').trim()
      const suggestionInsertText = pickSuggestionInsertText(item)
      const shouldRestorePatch = Boolean(targetText && (aiState === 'succeeded' || revisedText))

      if (shouldRestorePatch) {
        const existingPatch = editor.getAppliedAiPatch(riskId)
        if (existingPatch && existingPatch.revisedText === revisedText) {
          restoredCount += 1
          continue
        }

        const applied = editor.applyAiPatch({
          patchId: riskId,
          targetText,
          revisedText,
          preserveRawTarget: isAggregateRiskLike(item),
          scroll: false
        })
        if (applied || editor.getAppliedAiPatch(riskId)) {
          restoredCount += 1
          continue
        }
      }

      if (suggestionInsertText) {
        const inserted = editor.addSuggestionInsertComment({
          riskId,
          suggestionText: suggestionInsertText,
          targetText,
          anchorText: String(item?.anchor_text || ''),
          evidenceText: String(item?.evidence_text || ''),
          clauseUids: getLocateClauseUidsForSuggestionInsert(item),
          scroll: false
        })
        if (inserted) restoredCount += 1
      }
    }

    if (restoredCount > 0 || acceptedItems.length === 0) {
      restoredAcceptedCommentRunRef.current = restoreKey
    }
  }, [runId, file, result, docEditorReady])

  const onAiEditRisk = useCallback(
    async (riskId: number | string, revisedText: string) => {
      if (!runId) throw new Error('当前没有可操作的 run_id')
      // New backend endpoint (optional). If unavailable (404), we fall back to local persistence.
      // Even if we previously detected legacy mode, we still probe once here so upgrades take effect.

      const resp = await fetch(`/api/reviews/${runId}/risks/${encodeURIComponent(String(riskId))}/ai_edit`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ revised_text: revisedText })
      })

      if (resp.status === 404) {
        aiEndpointModeRef.current = 'legacy'
        const err: any = new Error('Not Found')
        err.code = 404
        throw err
      }
      if (!resp.ok) {
        const text = await resp.text()
        throw new Error(text || '请求失败')
      }
      aiEndpointModeRef.current = 'new'
      const payload = (await resp.json()) as { item?: any }
      if (payload.item) mergeUpdatedRisk(riskId, payload.item)
    },
    [runId, mergeUpdatedRisk, result]
  )

  const onAiRejectRisk = useCallback(
    async (riskId: number | string) => {
      if (!runId) throw new Error('当前没有可操作的 run_id')

      const tryNew = async () => {
        const resp = await fetch(`/api/reviews/${runId}/risks/${encodeURIComponent(String(riskId))}/ai_reject`, {
          method: 'POST'
        })
        if (!resp.ok) {
          const text = await resp.text()
          throw new Error(text || '请求失败')
        }
        const payload = (await resp.json()) as { item?: any }
        return payload.item
      }

      if (aiEndpointModeRef.current === 'legacy') {
        await onRejectRisk(riskId)
        return
      }

      try {
        const updated = await tryNew()
        aiEndpointModeRef.current = 'new'
        if (updated) mergeUpdatedRisk(riskId, updated)
      } catch (e: any) {
        // fallback: treat as rejecting the whole risk (legacy)
        const msg = String(e?.message || e)
        if (msg.includes('404') || msg.includes('Not Found')) {
          aiEndpointModeRef.current = 'legacy'
          await onRejectRisk(riskId)
        } else {
          throw e
        }
      }
    },
    [runId, mergeUpdatedRisk, onRejectRisk]
  )

  const onAiApplyRisk = useCallback(
    async (riskId: number | string) => {
      if (!runId) throw new Error('当前没有可操作的 run_id')
      const resp = await fetch(`/api/reviews/${runId}/risks/${encodeURIComponent(String(riskId))}/ai_apply`, {
        method: 'POST'
      })
      if (!resp.ok) {
        const text = await resp.text()
        throw new Error(text || '请求失败')
      }
      const payload = (await resp.json()) as { item?: any }
      const updated = payload.item
      if (!updated) return
      const aiApply = updated.ai_apply || {}
      const patchRevisedText = String(aiApply.revised_text || '')
      const items = (result?.risk_result_validated?.risk_result?.risk_items || []) as any[]
      const found = items.find((it) => String(it?.risk_id) === String(riskId))
      const mergedRiskForPatch = {
        ...(found || {}),
        ...(updated || {}),
        ai_apply: {
          ...((found?.ai_apply && typeof found.ai_apply === 'object') ? found.ai_apply : {}),
          ...((updated?.ai_apply && typeof updated.ai_apply === 'object') ? updated.ai_apply : {})
        },
        ai_rewrite: {
          ...((found?.ai_rewrite && typeof found.ai_rewrite === 'object') ? found.ai_rewrite : {}),
          ...((updated?.ai_rewrite && typeof updated.ai_rewrite === 'object') ? updated.ai_rewrite : {})
        }
      }
      const patchTargetText = pickBestPatchTarget(mergedRiskForPatch, String(aiApply.target_text || ''))

      setResult((prev) => {
        if (!prev) return prev
        const nextItems = (prev.risk_result_validated?.risk_result?.risk_items || []).map((it) =>
          String(it.risk_id) === String(riskId) ? { ...it, ...updated } : it
        )
        return {
          ...prev,
          risk_result_validated: {
            ...prev.risk_result_validated,
            risk_result: {
              ...prev.risk_result_validated.risk_result,
              risk_items: nextItems
            }
          }
        }
      })
      setHistoryEntries((entries) =>
        entries.map((entry) => {
          if (entry.run_id !== runId || !entry.result) return entry
          const nextItems = (entry.result.risk_result_validated?.risk_result?.risk_items || []).map((it) =>
            String(it.risk_id) === String(riskId) ? { ...it, ...updated } : it
          )
          return {
            ...entry,
            result: {
              ...entry.result,
              risk_result_validated: {
                ...entry.result.risk_result_validated,
                risk_result: {
                  ...entry.result.risk_result_validated.risk_result,
                  risk_items: nextItems
                }
              }
            },
            updated_at: new Date().toISOString()
          }
        })
      )
      if (patchTargetText && patchRevisedText) {
        editorRef.current?.applyAiPatch({
          patchId: riskId,
          targetText: patchTargetText,
          revisedText: patchRevisedText,
          preserveRawTarget: isAggregateRiskLike(mergedRiskForPatch)
        })
      }
    },
    [runId]
  )

  // Auto AI generation is allowed only for the run started in the current session.
  // Arbitrary historical records remain guarded by newRunIdRef + autoAiTriggeredRef.

  const resetReviewWorkspace = useCallback(() => {
    setRouteHydratingRunId(null)
    setIsReviewing(false)
    handleUploadFileChange(null)
    setRunId(null)
    setMeta(null)
    setResult(null)
    setEdits([])
    removeLocalValue(ACTIVE_RUN_ID_STORAGE_KEY)
  }, [handleUploadFileChange])

  const goUploadPage = useCallback(() => {
    navigate(pathForNav('upload'))
    resetReviewWorkspace()
  }, [navigate, resetReviewWorkspace])

  useEffect(() => {
    const prevNav = navFromPathname(prevPathnameRef.current)
    const nextNav = navFromPathname(location.pathname)
    if (prevNav === 'result' && nextNav === 'upload') {
      resetReviewWorkspace()
    }
    prevPathnameRef.current = location.pathname
  }, [location.pathname, resetReviewWorkspace])

  const goHistoryPage = useCallback(() => {
    navigate(pathForNav('history'))
    setIsReviewing(false)
  }, [navigate])

  const onSelectMainNav = useCallback(
    (key: NavKey) => {
      if (key === 'upload') {
        goUploadPage()
        return
      }
      if (key === 'history') {
        goHistoryPage()
        return
      }
      if (key === 'waiting' && !runId) {
        goUploadPage()
        return
      }
      if (key === 'waiting' && runId) {
        navigate(buildReviewPath(runId))
        return
      }
      if (key === 'result' && runId) {
        navigate(buildReviewPath(runId))
        return
      }
      if (key === 'result' && !runId) {
        goUploadPage()
        return
      }
      navigate(pathForNav(key))
    },
    [goHistoryPage, goUploadPage, runId, navigate]
  )

  const latestReview = historyEntries[0] || null
  const recentHistory = historyEntries.slice(0, 4)

  return (
    <>
      {activeNav === 'result' ? (
      // Review page: reuse the original (uploaded) review modules (TopBar/DocumentEditor/RiskPanel)
      // but DO NOT show the left sidebar. The page gets its own full-screen surface.
      <div className="legacyReview">
        <div className="reviewOnlyShell">
          <main className="contentShell">
            <div className="reviewWorkspace">
              <TopBar
                file={file}
                fileName={result?.file_name || meta?.file_name || file?.name || routeRunId || null}
                statusText={isRouteHydrating ? '' : statusText}
                runId={runId || routeRunId}
                riskCount={riskCount}
                riskStats={riskStats}
                isReviewing={isReviewing}
                onBack={() => {
                  if (prevNavRef.current === 'upload') {
                    goUploadPage()
                    return
                  }
                  if (prevNavRef.current === 'history') {
                    goHistoryPage()
                    return
                  }
                  navigate(pathForNav(prevNavRef.current))
                }}
                onGoUpload={goUploadPage}
                onGoHistory={goHistoryPage}
                downloadUrl={result?.download_url || null}
                onAcceptAllRisks={onAcceptAllRisks}
                canAcceptAllRisks={pendingRiskCount > 0}
              />

              <div className="mainGrid">
                <section className="docPane glassPane">
                  <div className="paneHeader">
                    <div className="paneTitle">合同原件</div>
                  </div>

                  <DocumentEditor
                    ref={editorRef}
                    file={file}
                    edits={edits}
                    onEditsChange={setEdits}
                    onReadyChange={setDocEditorReady}
                    clauseTextByUid={clauseTextByUid}
                    className="docEditor"
                  />
                </section>

                <aside className="riskPane glassPane">
                  {isRouteHydrating ? null : result == null ? (
                    <ReviewProgress
                      meta={meta}
                      runId={runId}
                      onGoUpload={goUploadPage}
                      onGoHistory={goHistoryPage}
                      onRestart={goUploadPage}
                    />
                  ) : (
                    <RiskPanel
                      result={result}
                      runId={runId}
                      riskStats={riskStats}
                      onLocateRisk={onLocateRisk}
                      onAcceptRisk={onAcceptRisk}
                      onRejectRisk={onRejectRisk}
                      onSetRiskStatus={onSetRiskStatus}
                      onAcceptAllRisks={onAcceptAllRisks}
                      onUndoAcceptAllRisks={onUndoAcceptAllRisks}
                      canUndoAcceptAllRisks={lastAcceptAllRiskIds.length > 0 || acceptedRiskIds.length > 0}
                      onAiApplyRisk={onAiApplyRisk}
                      onAiAcceptRisk={onAiAcceptRisk}
                      onAiEditRisk={onAiEditRisk}
                      onAiRejectRisk={onAiRejectRisk}
                    />
                  )}
                </aside>
              </div>
            </div>
          </main>
        </div>
      </div>
      ) : (
      <div className="appShell">
        <ModernSideNav
          activeNav={activeNav}
          onSelect={onSelectMainNav}
          recentItems={recentHistory}
          activeRunId={runId}
          onOpenRecent={async (item) => {
            try {
              await openSessionReview(item)
            } catch (e) {
              alert(`打开审查记录失败：${String(e)}`)
            }
          }}
        />

        <main className={`contentShell ${activeNav === 'upload' ? 'contentShell--noScroll' : ''}`}>
          <GlobalTopBar />

          {activeNav === 'upload' ? (
            <UploadDashboard
              file={file}
              setFile={handleUploadFileChange}
              isReviewing={isReviewing}
              reviewSide={selectedReviewSide}
              onReviewSideChange={handleReviewSideChange}
              onStartReview={async () => {
                try {
                  await startReview()
                } catch (e) {
                  alert(`发起审查失败：${String(e)}`)
                }
              }}
              latestReview={latestReview}
              recentItems={recentHistory}
              stats={historyStats}
              onOpenLatest={async () => {
                if (!latestReview) return
                try {
                  await openSessionReview(latestReview)
                } catch (e) {
                  alert(`打开历史记录失败：${String(e)}`)
                }
              }}
              onOpenHistory={goHistoryPage}
            />
          ) : null}

          {activeNav === 'history' ? (
            <ReviewHistoryPanel
              items={historyEntries}
              stats={historyStats}
              latestReview={latestReview}
              onOpen={async (item) => {
                try {
                  await openSessionReview(item)
                } catch (e) {
                  alert(`打开历史记录失败：${String(e)}`)
                }
              }}
              onStartNew={goUploadPage}
            />
          ) : null}
        </main>
      </div>
      )}
      <AlertDialog
        open={dialog.open}
        title={dialog.title}
        message={dialog.message}
        onClose={() => setDialog((prev) => ({ ...prev, open: false }))}
      />
    </>
  )
}
