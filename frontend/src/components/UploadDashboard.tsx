import React, { useEffect, useRef, useState } from 'react'
import {
  Check,
  ChevronDown,
  CheckCircle2,
  FileText,
  Lightbulb,
  ShieldCheck,
  UploadCloud,
  X,
  Zap,
  PenTool,
} from 'lucide-react'
import type { AnalysisScopeOption, ReviewHistoryItem, ReviewSideOption } from '../types'

const reviewSideCopy: Record<ReviewSideOption, {
  label: string
  title: string
  description: string
}> = {
  甲方: {
    label: '甲方（采购方/付款方）',
    title: '甲方（采购方/付款方）',
    description: '侧重于保护付款权益、控制违约风险、确保交付质量。'
  },
  乙方: {
    label: '乙方（供应商/收款方）',
    title: '乙方（供应商/收款方）',
    description: '侧重于保障收款权利、限制过度责任、明确验收标准。'
  }
}

const analysisScopeCopy: Record<AnalysisScopeOption, {
  title: string
  description: string
}> = {
  full_detail: {
    title: '深度审查',
    description: '输出全部风险点，并生成完整的依据与 AI 改写建议。'
  },
  high_risk_only: {
    title: '仅高风险',
    description: '聚焦高风险项，减少低中风险干扰，便于快速决策。'
  }
}

const reviewSideGroup = {
  title: '请选择您的审查立场',
  ariaLabel: '审查立场选择',
  options: ['甲方', '乙方'] as ReviewSideOption[]
} as const

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
  const scopeMenuRef = useRef<HTMLDivElement | null>(null)
  const [isDragActive, setIsDragActive] = useState(false)
  const [isScopeMenuOpen, setIsScopeMenuOpen] = useState(false)

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

  useEffect(() => {
    if (!isScopeMenuOpen) return

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null
      if (target && scopeMenuRef.current?.contains(target)) return
      setIsScopeMenuOpen(false)
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setIsScopeMenuOpen(false)
      }
    }

    document.addEventListener('pointerdown', handlePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [isScopeMenuOpen])

  useEffect(() => {
    if (props.isReviewing || props.isSubmittingReview) {
      setIsScopeMenuOpen(false)
    }
  }, [props.isReviewing, props.isSubmittingReview])

  const hasFile = Boolean(props.file)
  const isInteractionLocked = props.isReviewing || props.isSubmittingReview
  const fileSizeLabel = formatFileSize(props.file?.size)
  const selectedScopeCopy = analysisScopeCopy[props.analysisScope]

  const handleUploadCardClick = () => {
    if (isInteractionLocked) return
    if (!hasFile) {
      resetInputValue()
      inputRef.current?.click()
    }
  }

  const renderMatrixSection = () => (
    <div className={`matrixSection ${hasFile ? 'matrixSection--uploaded' : ''}`}>
      <div className="matrixHeader">
        <h3 className="matrixTitle">
          <Zap size={20} className="text-yellow-500 fill-yellow-500" /> 核心能力矩阵
        </h3>
        <p className="matrixSubtitle">全方位、智能化的合同审查体验</p>
      </div>

      <div className="matrixGrid">
        <div className="matrixCard group">
          <div className="matrixIcon matrixIcon--blue">
            <ShieldCheck size={24} />
          </div>
          <h4 className="matrixCardTitle">多维风险识别</h4>
          <ul className="matrixList">
            <li className="flex items-start gap-2 text-[11px] md:text-xs text-gray-600"><CheckCircle2 size={14} className="text-[#00b365] mt-0.5 shrink-0" /> 通用合同风险识别</li>
            <li className="flex items-start gap-2 text-[11px] md:text-xs text-gray-600"><CheckCircle2 size={14} className="text-[#00b365] mt-0.5 shrink-0" /> 行业特定风险识别</li>
            <li className="flex items-start gap-2 text-[11px] md:text-xs text-gray-600"><CheckCircle2 size={14} className="text-[#00b365] mt-0.5 shrink-0" /> 合同主体风险识别</li>
          </ul>
        </div>

        <div className="matrixCard group">
          <div className="matrixIcon matrixIcon--amber">
            <Lightbulb size={24} />
          </div>
          <h4 className="matrixCardTitle">智能审查建议</h4>
          <ul className="matrixList">
            <li className="flex items-start gap-2 text-[11px] md:text-xs text-gray-600"><CheckCircle2 size={14} className="text-[#00b365] mt-0.5 shrink-0" /> 快速定位合同风险点</li>
            <li className="flex items-start gap-2 text-[11px] md:text-xs text-gray-600"><CheckCircle2 size={14} className="text-[#00b365] mt-0.5 shrink-0" /> 智能风险提示与建议</li>
            <li className="flex items-start gap-2 text-[11px] md:text-xs text-gray-600"><CheckCircle2 size={14} className="text-[#00b365] mt-0.5 shrink-0" /> 智能推荐权威参考依据</li>
          </ul>
        </div>

        <div className="matrixCard group">
          <div className="matrixIcon matrixIcon--purple">
            <PenTool size={24} />
          </div>
          <h4 className="matrixCardTitle">AI 辅助改写</h4>
          <ul className="matrixList">
            <li className="flex items-start gap-2 text-[11px] md:text-xs text-gray-600"><CheckCircle2 size={14} className="text-[#00b365] mt-0.5 shrink-0" /> AI 智能改写合同原文</li>
            <li className="flex items-start gap-2 text-[11px] md:text-xs text-gray-600"><CheckCircle2 size={14} className="text-[#00b365] mt-0.5 shrink-0" /> 保持法务专业术语准确性</li>
            <li className="flex items-start gap-2 text-[11px] md:text-xs text-gray-600"><CheckCircle2 size={14} className="text-[#00b365] mt-0.5 shrink-0" /> 支持改写内容人工二次修改</li>
          </ul>
        </div>
      </div>
    </div>
  )

  const renderAnalysisScopeDropdown = () => (
    <div
      ref={scopeMenuRef}
      className="uploadScopeDock"
      onClick={(event) => event.stopPropagation()}
    >
      <button
        type="button"
        className="uploadScopeButton"
        aria-haspopup="listbox"
        aria-expanded={isScopeMenuOpen}
        aria-label="审查范围选择"
        onClick={() => setIsScopeMenuOpen((open) => !open)}
        disabled={isInteractionLocked}
      >
        <span className="uploadScopeButtonIcon">
          <FileText size={14} />
        </span>
        <span className="uploadScopeButtonValue">{selectedScopeCopy.title}</span>
        <ChevronDown
          size={14}
          className={`uploadScopeButtonCaret ${isScopeMenuOpen ? 'uploadScopeButtonCaret--open' : ''}`}
        />
      </button>

      {isScopeMenuOpen ? (
        <div className="uploadScopeMenu" role="listbox" aria-label="审查范围选项">
          {analysisScopeOptions.map((scope) => {
            const active = props.analysisScope === scope
            const copy = analysisScopeCopy[scope]
            return (
              <button
                key={scope}
                type="button"
                role="option"
                aria-selected={active}
                className={`uploadScopeOption ${active ? 'uploadScopeOption--active' : ''}`}
                onClick={() => {
                  props.onAnalysisScopeChange(scope)
                  setIsScopeMenuOpen(false)
                }}
                disabled={isInteractionLocked}
              >
                <span className={`uploadScopeOptionCheck ${active ? 'uploadScopeOptionCheck--active' : ''}`}>
                  <Check size={13} />
                </span>
                <span className="uploadScopeOptionMeta">
                  <span className="uploadScopeOptionTitle">{copy.title}</span>
                  <span className="uploadScopeOptionDesc">{copy.description}</span>
                </span>
              </button>
            )
          })}
        </div>
      ) : null}
    </div>
  )

  return (
    <div className={`dashboardPage ${hasFile ? 'dashboardPage--withFile' : ''}`}>
      <div className={`dashboardScroll ${hasFile ? 'dashboardScroll--uploaded' : ''}`}>
        <div className={`dashboardContentLane ${hasFile ? 'dashboardContentLane--uploaded' : ''}`}>
          <div className={`uploadHero shrink-0 ${hasFile ? 'uploadHero--compact' : ''}`}>
            <div className="uploadHeroLogo">
              <div className="w-10 h-10 bg-[#00b365] text-white rounded-full flex items-center justify-center font-bold mr-3">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-6 h-6"><path d="M2 12h4l2-3 4 6 2-3h4"/></svg>
              </div>
              <span className="font-bold text-[28px] md:text-[32px] tracking-wide text-gray-900">CHECKUP</span>
            </div>
            {!hasFile ? <div className="uploadHeroSubtitle">合同全能助手，AI赋能读/写/审</div> : null}
          </div>

          <div className={`uploadCardContainer w-full shrink-0 ${hasFile ? 'uploadCardContainer--uploaded' : 'max-w-[640px]'}`}>
            <div
              className={`uploadCard ${isDragActive ? 'uploadCard--active' : ''} ${hasFile ? 'uploadCard--uploaded' : ''}`}
              onClick={handleUploadCardClick}
              onDragOver={(event) => {
                event.preventDefault()
                if (!hasFile && !isInteractionLocked) setIsDragActive(true)
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
                onChange={(event) => {
                  pickFile(event.target.files?.[0] || null)
                  event.target.value = ''
                }}
              />

              {!hasFile ? (
                <>
                  <div className="uploadIconWrap">
                    <UploadCloud size={32} className="text-[#00b365]" />
                  </div>
                  <div className="text-base md:text-lg font-medium text-gray-700 mb-2 md:mb-3 text-center">
                    拖拽或复制合同文件，或 <span className="text-[#00b365]">选择文件</span>
                  </div>
                  <div className="text-xs md:text-sm text-gray-400 flex items-center justify-center gap-2">
                    支持 <span className="inline-flex items-center gap-1 bg-gray-50 px-2 py-1 rounded border border-gray-200 text-[10px] md:text-xs font-medium mx-1"><FileText size={12} className="text-blue-600" /> DOCX</span>
                  </div>
                </>
              ) : (
                <div className="postUploadReviewPanel" onClick={(event) => event.stopPropagation()}>
                  <div className="postUploadFileRow">
                    <div className="postUploadFileIcon">
                      <FileText size={28} strokeWidth={2.15} />
                    </div>
                    <div className="postUploadFileMeta">
                      <div className="postUploadFileName" title={props.file?.name || ''}>{props.file?.name}</div>
                      <div className="postUploadFileInfo">{fileSizeLabel} · DOCX</div>
                    </div>
                    <button
                      type="button"
                      className="postUploadRemoveBtn"
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

                  <div className="postUploadDivider" />

                  <div className="postUploadConfigGrid">
                    <section className="postUploadSelectorGroup">
                      <div className="postUploadSelectorTitle">
                        <span className="postUploadSelectorBar" />
                        <span>{reviewSideGroup.title}</span>
                      </div>

                      <div className="postUploadSideGrid" role="radiogroup" aria-label={reviewSideGroup.ariaLabel}>
                        {reviewSideGroup.options.map((side) => {
                          const active = props.reviewSide === side
                          const copy = reviewSideCopy[side]
                          return (
                            <button
                              key={side}
                              type="button"
                              role="radio"
                              aria-checked={active}
                              className={`postUploadSideCard ${active ? 'postUploadSideCard--active' : ''}`}
                              onClick={() => props.onReviewSideChange(side)}
                              disabled={props.isReviewing}
                            >
                              <div className="postUploadSideCardHeader">
                                <div className="postUploadSideCardTitle">{copy.title}</div>
                                <span className={`postUploadRadio ${active ? 'postUploadRadio--active' : ''}`}>
                                  <span className="postUploadRadioDot" />
                                </span>
                              </div>
                              <div className="postUploadSideCardDesc">{copy.description}</div>
                            </button>
                          )
                        })}
                      </div>
                    </section>
                  </div>
                </div>
              )}

              {renderAnalysisScopeDropdown()}
            </div>
          </div>

          <div className="uploadActions shrink-0">
            <button
              className="startReviewBtn"
              disabled={!props.file || !props.reviewSide || isInteractionLocked}
              onClick={props.onStartReview}
            >
              {props.isSubmittingReview ? '提交中…' : props.isReviewing ? '审查中…' : '开始智能审查'}
            </button>
          </div>

          {renderMatrixSection()}
        </div>
      </div>
    </div>
  )
}
