import React, { useEffect, useRef, useState } from 'react'
import {
  ArrowRight,
  FileText,
  Upload,
  X,
} from 'lucide-react'
import type { AnalysisScopeOption, ReviewHistoryItem, ReviewSideOption } from '../types'

const reviewSideCopy: Record<ReviewSideOption, {
  title: string
  description: string
}> = {
  甲方: {
    title: '甲方视角',
    description: '作为合同发起方，更关注权益保障'
  },
  乙方: {
    title: '乙方视角',
    description: '作为合同相对方，更关注责任限制'
  }
}

const analysisScopeCopy: Record<AnalysisScopeOption, {
  title: string
  description: string
}> = {
  full_detail: {
    title: '深度审查',
    description: '全面审查合同条款，识别各类风险'
  },
  high_risk_only: {
    title: '仅高风险',
    description: '聚焦高风险条款，快速定位关键问题'
  }
}

const reviewSideOptions = ['甲方', '乙方'] as ReviewSideOption[]
const analysisScopeOptions = ['full_detail', 'high_risk_only'] as AnalysisScopeOption[]

function formatFileSize(size?: number) {
  const safeSize = Number(size || 0)
  if (!Number.isFinite(safeSize) || safeSize <= 0) return '—'

  const units = ['B', 'KB', 'MB', 'GB']
  let value = safeSize
  let unitIndex = 0

  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex += 1
  }

  const fractionDigits = value >= 100 || unitIndex === 0 ? 0 : value >= 10 ? 1 : 2
  return `${value.toFixed(fractionDigits)} ${units[unitIndex]}`
}

function LandingFileIcon() {
  return (
    <div className="landingUploadFileIcon" aria-hidden="true">
      <div className="landingUploadFileCorner" />
      <Upload size={31} strokeWidth={2.4} />
    </div>
  )
}

function OptionRadio(props: { active: boolean }) {
  return (
    <span className={`landingOptionRadio ${props.active ? 'landingOptionRadio--active' : ''}`} aria-hidden="true">
      <span className="landingOptionRadioDot" />
    </span>
  )
}

export function UploadDashboard(props: {
  file: File | null
  setFile: (file: File | null) => void
  isReviewing: boolean
  isSubmittingReview: boolean
  reviewSide: ReviewSideOption | null
  onReviewSideChange: (side: ReviewSideOption) => void
  analysisScope: AnalysisScopeOption
  onAnalysisScopeChange: (scope: AnalysisScopeOption) => void
  onStartReview: () => void
  latestReview: ReviewHistoryItem | null
  recentItems: ReviewHistoryItem[]
  stats: any
  onOpenLatest: () => void
  onOpenHistory: () => void
}) {
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [isDragActive, setIsDragActive] = useState(false)

  const resetInputValue = () => {
    if (inputRef.current) {
      inputRef.current.value = ''
    }
  }

  const pickFile = (nextFile: File | null) => {
    if (!nextFile) return
    props.setFile(nextFile)
  }

  useEffect(() => {
    if (!props.file) {
      resetInputValue()
    }
  }, [props.file])

  const hasFile = Boolean(props.file)
  const isInteractionLocked = props.isReviewing || props.isSubmittingReview
  const fileSizeLabel = formatFileSize(props.file?.size)

  const handleUploadClick = () => {
    if (isInteractionLocked) return
    resetInputValue()
    inputRef.current?.click()
  }

  return (
    <div className="dashboardPage landingHomePage">
      <div className="landingWave landingWave--left" aria-hidden="true" />
      <div className="landingWave landingWave--right" aria-hidden="true" />

      <div className="landingHomeScroll">
        <section className="landingHero" aria-labelledby="landing-hero-title">
          <h1 id="landing-hero-title" className="landingHeroTitle">让合同审查更专业、更高效</h1>
          <p className="landingHeroSubtitle">基于专业法律知识与实践经验，识别风险，提供条款建议，助力更安全的商业决策。</p>
        </section>

        <section className="landingUploadSection" aria-label="合同文件上传">
          <div
            role="button"
            tabIndex={isInteractionLocked ? -1 : 0}
            aria-disabled={isInteractionLocked}
            className={`landingUploadBox ${isDragActive ? 'landingUploadBox--active' : ''} ${hasFile ? 'landingUploadBox--hasFile' : ''} ${isInteractionLocked ? 'landingUploadBox--disabled' : ''}`}
            onClick={handleUploadClick}
            onKeyDown={(event) => {
              if (event.key !== 'Enter' && event.key !== ' ') return
              event.preventDefault()
              handleUploadClick()
            }}
            onDragOver={(event) => {
              event.preventDefault()
              if (!isInteractionLocked) setIsDragActive(true)
            }}
            onDragLeave={() => setIsDragActive(false)}
            onDrop={(event) => {
              event.preventDefault()
              setIsDragActive(false)
              const nextFile = event.dataTransfer.files?.[0] || null
              if (!isInteractionLocked) pickFile(nextFile)
            }}
          >
            <input
              ref={inputRef}
              type="file"
              accept=".docx"
              className="hiddenInput"
              onClick={(event) => event.stopPropagation()}
              onChange={(event) => {
                pickFile(event.target.files?.[0] || null)
                event.target.value = ''
              }}
            />

            {!hasFile ? (
              <>
                <LandingFileIcon />
                <div className="landingUploadTitle">拖拽合同到此处，开始本地隐私审查</div>
              </>
            ) : (
              <div className="landingSelectedFile" onClick={(event) => event.stopPropagation()}>
                <div className="landingSelectedFileIcon"><FileText size={28} /></div>
                <div className="landingSelectedFileMeta">
                  <div className="landingSelectedFileName" title={props.file?.name || ''}>{props.file?.name}</div>
                  <div className="landingSelectedFileInfo">{fileSizeLabel} · DOCX</div>
                </div>
                <button
                  type="button"
                  className="landingSelectedFileRemove"
                  aria-label="移除文件"
                  onClick={() => {
                    resetInputValue()
                    props.setFile(null)
                  }}
                  disabled={isInteractionLocked}
                >
                  <X size={20} />
                </button>
              </div>
            )}
          </div>

          <div className="landingSecurityHint">本地运行环境内完成审查，隐私数据全程受保护，审查后可随时删除。</div>
        </section>

        <section className="landingOptionsPanel" aria-label="审查设置">
          <div className="landingOptionRow">
            <div className="landingOptionLabelBlock">
              <h2 className="landingOptionGroupTitle">审查视角</h2>
            </div>
            <div className="landingOptionCards" role="radiogroup" aria-label="审查视角">
              {reviewSideOptions.map((side) => {
                const copy = reviewSideCopy[side]
                const active = props.reviewSide === side
                return (
                  <button
                    key={side}
                    type="button"
                    role="radio"
                    aria-checked={active}
                    className={`landingOptionCard ${active ? 'landingOptionCard--active' : ''}`}
                    onClick={() => props.onReviewSideChange(side)}
                    disabled={isInteractionLocked}
                  >
                    <OptionRadio active={active} />
                    <span className="landingOptionCardMeta">
                      <span className="landingOptionCardTitle">{copy.title}</span>
                      <span className="landingOptionCardDesc">{copy.description}</span>
                    </span>
                  </button>
                )
              })}
            </div>
          </div>

          <div className="landingOptionRow">
            <div className="landingOptionLabelBlock">
              <h2 className="landingOptionGroupTitle">审查范围</h2>
            </div>
            <div className="landingOptionCards" role="radiogroup" aria-label="审查范围">
              {analysisScopeOptions.map((scope) => {
                const copy = analysisScopeCopy[scope]
                const active = props.analysisScope === scope
                return (
                  <button
                    key={scope}
                    type="button"
                    role="radio"
                    aria-checked={active}
                    className={`landingOptionCard ${active ? 'landingOptionCard--active' : ''}`}
                    onClick={() => props.onAnalysisScopeChange(scope)}
                    disabled={isInteractionLocked}
                  >
                    <OptionRadio active={active} />
                    <span className="landingOptionCardMeta">
                      <span className="landingOptionCardTitle">{copy.title}</span>
                      <span className="landingOptionCardDesc">{copy.description}</span>
                    </span>
                  </button>
                )
              })}
            </div>
          </div>
        </section>

        <section className="landingActions" aria-label="开始审查">
          <button
            type="button"
            className="landingStartBtn"
            disabled={!props.file || !props.reviewSide || isInteractionLocked}
            onClick={props.onStartReview}
          >
            <span>{props.isSubmittingReview ? '提交中…' : props.isReviewing ? '审查中…' : '开始审查'}</span>
            <ArrowRight size={21} strokeWidth={2.3} />
          </button>
        </section>
      </div>
    </div>
  )
}
