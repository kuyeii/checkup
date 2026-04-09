import React, { useMemo } from 'react'
import type { ReviewMeta } from '../types'

function inferStage(step: string) {
  const s = (step || '').toLowerCase()
  if (s.includes('上传') || s.includes('等待') || s.includes('排队') || s.includes('queued')) return 0
  if (s.includes('解析') || s.includes('提取') || s.includes('拆分') || s.includes('分段') || s.includes('段落') || s.includes('结构') || s.includes('脱敏')) return 1
  if (s.includes('风险') || s.includes('识别') || s.includes('校验') || s.includes('check')) return 2
  if (s.includes('生成') || s.includes('输出') || s.includes('docx') || s.includes('下载')) return 3
  return 1
}

function computeProgress(meta: ReviewMeta | null) {
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

type TaskState = 'done' | 'active' | 'todo' | 'failed'

function resolveTaskStates(percent: number, status: ReviewMeta['status'] | undefined) {
  const flatTasks = REVIEW_GROUPS.flatMap((g) => g.tasks)
  const total = Math.max(1, flatTasks.length)
  const roughIndex = Math.floor((Math.max(1, Math.min(99, percent)) / 100) * total)
  const activeIndex = Math.max(0, Math.min(total - 1, roughIndex))

  const states: TaskState[] = flatTasks.map((_, idx) => {
    if (status === 'completed') return 'done'
    if (status === 'failed') {
      if (idx < activeIndex) return 'done'
      if (idx === activeIndex) return 'failed'
      return 'todo'
    }
    if (idx < activeIndex) return 'done'
    if (idx === activeIndex) return 'active'
    return 'todo'
  })

  return { flatTasks, states }
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
  const prog = baseProg
  const { states } = useMemo(() => resolveTaskStates(prog.percent, status), [prog.percent, status])
  let cursor = 0

  return (
    <div className="progressWrap">
      <div className="progressCard">
        <div className="progressHeader">
          <div>
            <div className="progressTitle">正在审查合同</div>
            <div className="progressSub">{prog.label}</div>
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
                            <span className={`progressTaskRing ${state === 'active' && isWaiting ? 'progressTaskRing--spin' : ''}`} />
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
