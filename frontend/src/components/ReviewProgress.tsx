import React, { useEffect, useMemo, useRef, useState } from 'react'
import type { ReviewMeta } from '../types'

const REVIEW_PROGRESS_UI_STATE_STORAGE_KEY = 'markup:reviewProgressUiStateByRun'

type PersistedReviewProgressUiState = {
  runId: string
  introStartedAt: number
  highestActiveIndex: number
  savedAt: string
}

function readStoredProgressUiStates() {
  try {
    const raw = window.sessionStorage.getItem(REVIEW_PROGRESS_UI_STATE_STORAGE_KEY)
    if (!raw) return {} as Record<string, PersistedReviewProgressUiState>
    const parsed = JSON.parse(raw) as Record<string, PersistedReviewProgressUiState>
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {} as Record<string, PersistedReviewProgressUiState>
  }
}

function readStoredProgressUiState(runId?: string | null) {
  const key = String(runId || '').trim()
  if (!key) return null
  const states = readStoredProgressUiStates()
  const state = states[key]
  if (!state) return null
  if (state.runId !== key) return null
  return state
}

function writeStoredProgressUiState(
  runId: string,
  state: { introStartedAt: number; highestActiveIndex: number }
) {
  const key = String(runId || '').trim()
  if (!key) return
  try {
    const states = readStoredProgressUiStates()
    states[key] = {
      runId: key,
      introStartedAt: state.introStartedAt,
      highestActiveIndex: state.highestActiveIndex,
      savedAt: new Date().toISOString()
    }
    window.sessionStorage.setItem(REVIEW_PROGRESS_UI_STATE_STORAGE_KEY, JSON.stringify(states))
  } catch {
    return
  }
}

function inferStage(step: string) {
  const s = (step || '').toLowerCase()
  if (s.includes('上传') || s.includes('等待') || s.includes('排队') || s.includes('queued')) return 0
  if (s.includes('解析') || s.includes('提取') || s.includes('拆分') || s.includes('分段') || s.includes('段落') || s.includes('结构') || s.includes('脱敏')) return 1
  if (s.includes('风险') || s.includes('识别') || s.includes('校验') || s.includes('check')) return 2
  if (s.includes('生成') || s.includes('输出') || s.includes('docx') || s.includes('下载')) return 3
  return 1
}

export function computeProgress(meta: ReviewMeta | null) {
  if (!meta) return { percent: 8, stage: 0, label: '准备中…' }
  if (meta.status === 'failed') return { percent: 100, stage: 3, label: meta.error || '审查失败' }
  if (meta.status === 'completed') return { percent: 100, stage: 3, label: '审查完成' }
  if (typeof meta.progress === 'number' && Number.isFinite(meta.progress)) {
    const percent = Math.max(1, Math.min(99, Math.round(meta.progress)))
    const stage = percent >= 85 ? 3 : percent >= 60 ? 2 : percent >= 30 ? 1 : 0
    return { percent, stage, label: meta.step || '处理中…' }
  }
  const step = meta.step || (meta.status === 'queued' ? '排队中…' : '处理中…')
  const stage = inferStage(step)
  const stageToPercent = [12, 38, 68, 88]
  const percent = stageToPercent[stage] ?? 38
  return { percent, stage, label: step }
}

const REVIEW_GROUPS = [
  { title: '文本切分', tasks: ['解析合同结构并切分段落'] },
  { title: '隐私脱敏', tasks: ['识别并处理敏感信息'] },
  { title: '风险审查', tasks: ['审查权利义务与违约责任', '审查程序性条款与文字符号风险'] },
  { title: '结果生成', tasks: ['汇总风险依据与修订建议', '生成结构化审查结果'] }
]

const FLAT_TASKS = REVIEW_GROUPS.flatMap((group) => group.tasks)
const INTRO_FIRST_STEP_MS = 2000
const INTRO_TOTAL_MS = 4000
const INTRO_SECOND_TASK_INDEX = 1

const INTRO_LABELS = [
  '正在解析合同结构并切分段落…',
  '正在识别并处理敏感信息…'
] as const

type TaskState = 'done' | 'active' | 'todo' | 'failed'
type IntroPhase = 0 | 1 | 2

function resolveIntroPhaseFromStart(introStartedAt: number): IntroPhase {
  const elapsed = Math.max(0, Date.now() - introStartedAt)
  if (elapsed >= INTRO_TOTAL_MS) return 2
  if (elapsed >= INTRO_FIRST_STEP_MS) return 1
  return 0
}

function resolveActiveIndex(percent: number) {
  const total = Math.max(1, FLAT_TASKS.length)
  const clampedPercent = Math.max(1, Math.min(99, percent))
  const roughIndex = Math.floor((clampedPercent / 100) * total)
  return Math.max(0, Math.min(total - 1, roughIndex))
}

function buildTaskStates(activeIndex: number, status: ReviewMeta['status'] | undefined) {
  return FLAT_TASKS.map((_, idx) => {
    if (status === 'completed') return 'done' as const
    if (status === 'failed') {
      if (idx < activeIndex) return 'done' as const
      if (idx === activeIndex) return 'failed' as const
      return 'todo' as const
    }
    if (idx < activeIndex) return 'done' as const
    if (idx === activeIndex) return 'active' as const
    return 'todo' as const
  })
}

function buildIntroStates(phase: IntroPhase) {
  return FLAT_TASKS.map((_, idx) => {
    if (phase === 0) {
      if (idx === 0) return 'active' as const
      return 'todo' as const
    }
    if (idx === 0) return 'done' as const
    if (idx === 1) return 'active' as const
    return 'todo' as const
  })
}

export function ReviewProgress(props: {
  meta: ReviewMeta | null
  runId: string | null
  onGoUpload?: () => void
  onGoHistory?: () => void
  onRestart?: () => void
}) {
  const baseProg = useMemo(() => computeProgress(props.meta), [props.meta])
  const status = props.meta?.status
  const isWaiting = !status || status === 'queued' || status === 'running'
  const realActiveIndex = useMemo(() => resolveActiveIndex(baseProg.percent), [baseProg.percent])
  const progressUiRunId = useMemo(() => String(props.runId || '').trim(), [props.runId])

  const [introPhase, setIntroPhase] = useState<IntroPhase>(() => {
    if (!isWaiting) return 2
    if (realActiveIndex >= INTRO_SECOND_TASK_INDEX) return 2
    const persisted = readStoredProgressUiState(progressUiRunId)
    return persisted ? resolveIntroPhaseFromStart(persisted.introStartedAt) : 0
  })
  const highestActiveIndexRef = useRef(Math.max(readStoredProgressUiState(progressUiRunId)?.highestActiveIndex ?? 0, realActiveIndex))

  useEffect(() => {
    const persisted = readStoredProgressUiState(progressUiRunId)
    const persistedHighestActiveIndex = persisted?.highestActiveIndex ?? 0
    highestActiveIndexRef.current = Math.max(persistedHighestActiveIndex, realActiveIndex)

    if (!isWaiting || realActiveIndex >= INTRO_SECOND_TASK_INDEX) {
      setIntroPhase(2)
      if (progressUiRunId) {
        writeStoredProgressUiState(progressUiRunId, {
          introStartedAt: persisted?.introStartedAt ?? Date.now(),
          highestActiveIndex: highestActiveIndexRef.current
        })
      }
      return
    }

    const introStartedAt = persisted?.introStartedAt ?? Date.now()
    const nextPhase = resolveIntroPhaseFromStart(introStartedAt)
    setIntroPhase(nextPhase)

    if (progressUiRunId) {
      writeStoredProgressUiState(progressUiRunId, {
        introStartedAt,
        highestActiveIndex: highestActiveIndexRef.current
      })
    }

    const elapsed = Math.max(0, Date.now() - introStartedAt)
    const firstDelay = Math.max(0, INTRO_FIRST_STEP_MS - elapsed)
    const doneDelay = Math.max(0, INTRO_TOTAL_MS - elapsed)
    let firstTimer: number | null = null
    let doneTimer: number | null = null

    if (elapsed < INTRO_FIRST_STEP_MS) {
      firstTimer = window.setTimeout(() => setIntroPhase(1), firstDelay)
    }
    if (elapsed < INTRO_TOTAL_MS) {
      doneTimer = window.setTimeout(() => setIntroPhase(2), doneDelay)
    }

    return () => {
      if (firstTimer != null) window.clearTimeout(firstTimer)
      if (doneTimer != null) window.clearTimeout(doneTimer)
    }
  }, [progressUiRunId, isWaiting, realActiveIndex])

  const displayActiveIndex = useMemo(() => {
    if (introPhase === 0) return 0
    if (introPhase === 1) return INTRO_SECOND_TASK_INDEX
    return Math.max(highestActiveIndexRef.current, INTRO_SECOND_TASK_INDEX, realActiveIndex)
  }, [introPhase, realActiveIndex])

  useEffect(() => {
    highestActiveIndexRef.current = Math.max(highestActiveIndexRef.current, displayActiveIndex)
    if (!progressUiRunId) return
    const persisted = readStoredProgressUiState(progressUiRunId)
    writeStoredProgressUiState(progressUiRunId, {
      introStartedAt: persisted?.introStartedAt ?? Date.now(),
      highestActiveIndex: highestActiveIndexRef.current
    })
  }, [progressUiRunId, displayActiveIndex])

  const displayLabel = introPhase === 0 ? INTRO_LABELS[0] : introPhase === 1 ? INTRO_LABELS[1] : baseProg.label
  const states = useMemo(() => {
    if (introPhase === 0 || introPhase === 1) return buildIntroStates(introPhase)
    return buildTaskStates(displayActiveIndex, status)
  }, [displayActiveIndex, introPhase, status])
  const shouldSpinActiveTask = introPhase !== 2 || isWaiting

  let cursor = 0

  return (
    <div className="progressWrap">
      <div className="progressCard">
        <div className="progressHeader">
          <div>
            <div className="progressTitle">正在审查合同</div>
            <div className="progressSub">{displayLabel}</div>
          </div>
        </div>

        <div className="progressSteps">
          {REVIEW_GROUPS.map((group) => {
            return (
              <div key={group.title} className="progressGroup">
                <div className="progressGroupTitle">{group.title}</div>
                <div className="progressGroupTasks">
                  {group.tasks.map((task) => {
                    const state = states[cursor] || 'todo'
                    cursor += 1
                    return (
                      <div key={`${group.title}-${task}`} className={`progressTask progressTask--${state}`}>
                        <div className="progressTaskIcon" aria-hidden="true">
                          {state === 'done' ? (
                            <svg viewBox="0 0 16 16" className="progressTaskCheck">
                              <path d="M3 8.4 6.5 12 13 5.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                            </svg>
                          ) : (
                            <span className={`progressTaskRing ${state === 'active' && shouldSpinActiveTask ? 'progressTaskRing--spin' : ''}`} />
                          )}
                        </div>
                        <div className="progressTaskText">{task}</div>
                      </div>
                    )
                  })}
                </div>
              </div>
            )
          })}
        </div>

        {props.meta?.status === 'failed' ? (
          <div className="progressActions">
            <button className="btn btnPrimary" onClick={props.onRestart || props.onGoUpload}>
              重新上传
            </button>
            {props.onGoUpload ? (
              <button className="btn" onClick={props.onGoUpload}>
                返回首页
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  )
}
