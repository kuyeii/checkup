import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { DocumentEditor, DocumentEditorHandle } from './components/DocumentEditor'
import { ReviewHistoryPanel } from './components/ReviewHistoryPanel'
import { RiskPanel } from './components/RiskPanel'
import { SideNav, type NavKey } from './components/SideNav'
import { TopBar } from './components/TopBar'
import { UploadDashboard } from './components/UploadDashboard'
import type { EditSummary, ReviewHistoryItem, ReviewMeta, ReviewResultPayload } from './types'

async function sleep(ms: number) {
  await new Promise((r) => setTimeout(r, ms))
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

export default function App() {
  const editorRef = useRef<DocumentEditorHandle | null>(null)
  const [activeNav, setActiveNav] = useState<NavKey>('upload')
  const [file, setFile] = useState<File | null>(null)
  const [runId, setRunId] = useState<string | null>(null)
  const [meta, setMeta] = useState<ReviewMeta | null>(null)
  const [result, setResult] = useState<ReviewResultPayload | null>(null)
  const [isReviewing, setIsReviewing] = useState(false)
  const [edits, setEdits] = useState<EditSummary[]>([])
  const [historyEntries, setHistoryEntries] = useState<SessionReviewEntry[]>([])

  const riskHighlights = useMemo(() => {
    const items = result?.risk_result_validated?.risk_result?.risk_items || []
    const texts: string[] = []
    for (const r of items) {
      if (r.anchor_text) texts.push(r.anchor_text)
      if (r.evidence_text) texts.push(r.evidence_text)
    }
    return texts
      .map((t) => t.trim())
      .filter((t) => t.length >= 4)
      .slice(0, 200)
  }, [result])

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

  const refreshHistoryFromApi = useCallback(async () => {
    const resp = await fetch('/api/reviews/history?limit=30')
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
          updated_at: item.updated_at || prev?.updated_at || new Date().toISOString(),
          created_at: prev?.created_at || item.updated_at || new Date().toISOString(),
          available: item.document_ready ?? prev?.available ?? true,
          file: prev?.file ?? null,
          meta: prev?.meta ?? null,
          result: prev?.result ?? null
        })
      }
      return Array.from(byRunId.values()).sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)))
    })
  }, [])

  const openSessionReview = useCallback(async (item: ReviewHistoryItem) => {
    setActiveNav('result')
    setEdits([])

    let nextMeta = item.meta ?? null
    let nextFile = item.file ?? null
    let nextResult = item.result ?? null

    const statusResp = await fetch(`/api/reviews/${item.run_id}`)
    if (statusResp.ok) {
      nextMeta = (await statusResp.json()) as ReviewMeta
    }

    if (!nextFile) {
      const docResp = await fetch(`/api/reviews/${item.run_id}/document`)
      if (docResp.ok) {
        const blob = await docResp.blob()
        const fallbackName = nextMeta?.file_name || item.file_name || `${item.run_id}.docx`
        const fileName = pickFilenameFromDisposition(docResp.headers.get('content-disposition'), fallbackName)
        nextFile = new File([blob], fileName, { type: blob.type || 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' })
      }
    }

    if ((nextMeta?.status === 'completed' || item.status === 'completed') && !nextResult) {
      const resultResp = await fetch(`/api/reviews/${item.run_id}/result`)
      if (resultResp.ok) {
        nextResult = (await resultResp.json()) as ReviewResultPayload
      }
    }

    setFile(nextFile)
    setRunId(item.run_id)
    setMeta(nextMeta)
    setResult(nextResult)
    setIsReviewing((nextMeta?.status || item.status) === 'queued' || (nextMeta?.status || item.status) === 'running')

    setHistoryEntries((entries) =>
      upsertHistory(
        entries,
        item.run_id,
        (prev) => ({
          ...prev,
          file: nextFile,
          meta: nextMeta,
          result: nextResult,
          file_name: prev.file_name || nextFile?.name || nextMeta?.file_name || item.file_name,
          status: (nextMeta?.status || prev.status) as ReviewMeta['status'],
          summary: prev.summary || nextMeta?.step || prev.status,
          updated_at: new Date().toISOString(),
          available: true
        }),
        nextFile,
        nextMeta
      )
    )
  }, [])

  const startReview = useCallback(async () => {
    if (!file) return
    setIsReviewing(true)
    setResult(null)
    setMeta(null)
    setRunId(null)
    setEdits([])

    const form = new FormData()
    form.append('file', file)
    form.append('review_side', 'supplier')
    form.append('contract_type_hint', 'service_agreement')

    const resp = await fetch('/api/reviews', { method: 'POST', body: form })
    if (!resp.ok) {
      const text = await resp.text()
      throw new Error(text)
    }
    const data = (await resp.json()) as { run_id: string }
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
    setActiveNav('result')
  }, [file])

  useEffect(() => {
    let cancelled = false
    if (!runId) return

    ;(async () => {
      try {
        while (!cancelled) {
          const resp = await fetch(`/api/reviews/${runId}`)
          const m = (await resp.json()) as ReviewMeta
          if (cancelled) return
          setMeta(m)
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
                updated_at: new Date().toISOString(),
                available: true
              }),
              file,
              m
            )
          )

          if (m.status === 'completed') {
            const r = await fetch(`/api/reviews/${runId}/result`)
            const payload = (await r.json()) as ReviewResultPayload
            if (cancelled) return
            setResult(payload)
            setIsReviewing(false)
            setHistoryEntries((entries) =>
              upsertHistory(
                entries,
                runId,
                (prev) => ({
                  ...prev,
                  file: prev.file || file,
                  meta: m,
                  result: payload,
                  file_name: prev.file_name || file?.name || payload.file_name,
                  status: 'completed',
                  summary: payload.risk_result_validated?.error_message || `已完成 · ${payload.risk_result_validated?.risk_result?.risk_items?.length || 0} 个风险点`,
                  updated_at: new Date().toISOString(),
                  available: true
                }),
                file,
                m
              )
            )
            void refreshHistoryFromApi()
            break
          }
          if (m.status === 'failed') {
            setIsReviewing(false)
            void refreshHistoryFromApi()
            break
          }
          await sleep(1200)
        }
      } catch (e) {
        if (!cancelled) {
          setIsReviewing(false)
          const failedMeta = { run_id: runId, status: 'failed', error: String(e) } as ReviewMeta
          setMeta(failedMeta)
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
                updated_at: new Date().toISOString(),
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
    }
  }, [runId, file, refreshHistoryFromApi])

  useEffect(() => {
    void refreshHistoryFromApi()
  }, [refreshHistoryFromApi])

  const onLocateRisk = useCallback((opts: { anchorText?: string; evidenceText?: string; clauseUids?: string[] }) => {
    editorRef.current?.locateRisk(opts)
  }, [])

  const latestReview = historyEntries[0] || null

  return (
    <div className="appShell">
      <SideNav
        activeNav={activeNav}
        onSelect={setActiveNav}
        reviewCount={historyEntries.length}
        currentRunId={runId}
      />

      <main className="contentShell">
        {activeNav === 'upload' ? (
          <UploadDashboard
            file={file}
            setFile={setFile}
            isReviewing={isReviewing}
            onStartReview={async () => {
              try {
                await startReview()
              } catch (e) {
                alert(`发起审查失败：${String(e)}`)
              }
            }}
            latestReview={latestReview}
            onOpenLatest={async () => {
              if (!latestReview) return
              try {
                await openSessionReview(latestReview)
              } catch (e) {
                alert(`打开历史记录失败：${String(e)}`)
              }
            }}
            onOpenHistory={() => setActiveNav('history')}
          />
        ) : null}

        {activeNav === 'history' ? (
          <ReviewHistoryPanel
            items={historyEntries}
            onOpen={async (item) => {
              try {
                await openSessionReview(item)
              } catch (e) {
                alert(`打开历史记录失败：${String(e)}`)
              }
            }}
            onStartNew={() => setActiveNav('upload')}
          />
        ) : null}

        {activeNav === 'result' ? (
          <div className="reviewWorkspace">
            <TopBar
              file={file}
              statusText={statusText}
              runId={runId}
              riskCount={riskCount}
              isReviewing={isReviewing}
              onGoUpload={() => setActiveNav('upload')}
              onGoHistory={() => setActiveNav('history')}
              downloadUrl={result?.download_url || null}
            />

            <div className="mainGrid">
              <section className="docPane glassPane">
                <div className="paneHeader">
                  <div className="paneTitle">合同原件</div>
                  <div className="paneHint">支持原文定位、编辑修改和风险高亮。整体布局经过重新整理，更适合长时间审查。</div>
                </div>

                <DocumentEditor
                  ref={editorRef}
                  file={file}
                  edits={edits}
                  onEditsChange={setEdits}
                  riskHighlights={riskHighlights}
                  clauseTextByUid={clauseTextByUid}
                  className="docEditor"
                />
              </section>

              <aside className="riskPane glassPane">
                <RiskPanel result={result} onLocateRisk={onLocateRisk} />
              </aside>
            </div>
          </div>
        ) : null}
      </main>
    </div>
  )
}
