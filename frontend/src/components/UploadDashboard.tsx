import React, { useRef, useState } from 'react'
import { UploadCloud, FileText, ShieldCheck, Zap, Lightbulb, PenTool, CheckCircle2 } from 'lucide-react'
import type { ReviewHistoryItem } from '../types'

export function UploadDashboard(props: {
  file: File | null
  setFile: (file: File | null) => void
  isReviewing: boolean
  onStartReview: () => void
  latestReview: ReviewHistoryItem | null
  recentItems: ReviewHistoryItem[]
  stats: any
  onOpenLatest: () => void
  onOpenHistory: () => void
}) {
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [isDragActive, setIsDragActive] = useState(false)

  const pickFile = (nextFile: File | null) => {
    if (!nextFile) return
    props.setFile(nextFile)
  }

  return (
    <div className="dashboardPage">
      {/*
        This page lives inside a scroll container.
        If the inner wrapper uses flex + default shrink behavior, the upload card can get squashed
        and its content will be clipped. Keep the layout scroll-first (not fit-first).
      */}
      <div className="dashboardScroll flex flex-col items-center justify-start">
        {/*
          NOTE: Homepage must not scroll.
          Use responsive spacing (CSS clamp + height breakpoints) instead of zoom/scale.
          Keep utility spacing minimal so CSS can adapt reliably.
        */}
        <div className="uploadHero shrink-0">
          <div className="uploadHeroLogo">
            <div className="w-10 h-10 bg-[#00b365] text-white rounded-full flex items-center justify-center font-bold mr-3">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-6 h-6"><path d="M2 12h4l2-3 4 6 2-3h4"/></svg>
            </div>
            <span className="font-bold text-2xl md:text-3xl tracking-wide text-gray-900">CHECKUP</span>
          </div>
          <div className="uploadHeroSubtitle">合同全能助手，AI赋能读/写/审</div>
        </div>

        <div className="uploadCardContainer w-full max-w-3xl shrink-0">
          <div
            className={`uploadCard ${isDragActive ? 'uploadCard--active' : ''}`}
            onClick={() => inputRef.current?.click()}
            onDragOver={(event) => {
              event.preventDefault()
              setIsDragActive(true)
            }}
            onDragLeave={() => setIsDragActive(false)}
            onDrop={(event) => {
              event.preventDefault()
              setIsDragActive(false)
              const nextFile = event.dataTransfer.files?.[0] || null
              pickFile(nextFile)
            }}
          >
            <input
              ref={inputRef}
              type="file"
              accept=".docx"
              className="hiddenInput"
              onChange={(event) => pickFile(event.target.files?.[0] || null)}
            />
            
            <div className="uploadIconWrap">
              <UploadCloud size={32} className="text-[#00b365]" />
            </div>
            <div className="text-base md:text-lg font-medium text-gray-700 mb-2 md:mb-3 text-center">
              拖拽或复制合同文件，或 <span className="text-[#00b365]">选择文件</span>
            </div>
            <div className="text-xs md:text-sm text-gray-400 flex items-center justify-center gap-2">
              支持 <span className="inline-flex items-center gap-1 bg-gray-50 px-2 py-1 rounded border border-gray-200 text-[10px] md:text-xs font-medium mx-1"><FileText size={12} className="text-blue-600" /> DOCX</span>
            </div>
            {props.file && (
              <div className="selectedFilePill">
                已选择: {props.file.name}
              </div>
            )}
          </div>
        </div>

        <div className="uploadActions shrink-0">
          <button
            className="startReviewBtn"
            disabled={!props.file || props.isReviewing}
            onClick={props.onStartReview}
          >
            {props.isReviewing ? '审查中…' : '开始审查'}
          </button>
        </div>

        {/* Figma-style Filler Elements */}
        <div className="matrixSection shrink-0">
          <div className="matrixHeader">
            <h3 className="matrixTitle">
              <Zap size={22} className="text-yellow-500 fill-yellow-500" /> 核心能力矩阵
            </h3>
            <p className="matrixSubtitle">全方位、智能化的合同审查体验</p>
          </div>

          <div className="matrixGrid">
            {/* Card 1: Risk Identification */}
            <div className="matrixCard group">
              <div className="matrixIcon matrixIcon--blue">
                <ShieldCheck size={26} />
              </div>
              <h4 className="matrixCardTitle">多维风险识别</h4>
              <ul className="matrixList">
                <li className="flex items-start gap-2 text-[11px] md:text-xs text-gray-600"><CheckCircle2 size={14} className="text-[#00b365] mt-0.5 shrink-0" /> 通用合同风险识别</li>
                <li className="flex items-start gap-2 text-[11px] md:text-xs text-gray-600"><CheckCircle2 size={14} className="text-[#00b365] mt-0.5 shrink-0" /> 行业特定风险识别</li>
                <li className="flex items-start gap-2 text-[11px] md:text-xs text-gray-600"><CheckCircle2 size={14} className="text-[#00b365] mt-0.5 shrink-0" /> 合同主体风险识别</li>
              </ul>
            </div>

            {/* Card 2: Smart Suggestions */}
            <div className="matrixCard group">
              <div className="matrixIcon matrixIcon--amber">
                <Lightbulb size={26} />
              </div>
              <h4 className="matrixCardTitle">智能审查建议</h4>
              <ul className="matrixList">
                <li className="flex items-start gap-2 text-[11px] md:text-xs text-gray-600"><CheckCircle2 size={14} className="text-[#00b365] mt-0.5 shrink-0" /> 快速定位合同风险点</li>
                <li className="flex items-start gap-2 text-[11px] md:text-xs text-gray-600"><CheckCircle2 size={14} className="text-[#00b365] mt-0.5 shrink-0" /> 智能风险提示与建议</li>
                <li className="flex items-start gap-2 text-[11px] md:text-xs text-gray-600"><CheckCircle2 size={14} className="text-[#00b365] mt-0.5 shrink-0" /> 智能推荐权威参考依据</li>
              </ul>
            </div>

            {/* Card 3: AI Rewriting */}
            <div className="matrixCard group">
              <div className="matrixIcon matrixIcon--purple">
                <PenTool size={26} />
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
      </div>
    </div>
  )
}
