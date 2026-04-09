import React, { useRef } from 'react'
import type { ReviewHistoryItem } from '../types'

export function UploadDashboard(props: {
  file: File | null
  setFile: (file: File | null) => void
  isReviewing: boolean
  onStartReview: () => void
  latestReview: ReviewHistoryItem | null
  onOpenLatest: () => void
  onOpenHistory: () => void
}) {
  const inputRef = useRef<HTMLInputElement | null>(null)

  return (
    <div className="dashboardPage">
      <section className="heroPanel glassPane">
        <div className="eyebrow">合同审查工作台</div>
        <h1 className="heroTitle">把上传、审查和回看记录拆成清晰的导航层级。</h1>
        <p className="heroCopy">
          现在首页只负责开始新任务和浏览记录，真正的文档对照审查放在“当前结果”里。整体采用苹果式简约风格：大留白、轻玻璃感、低噪声。
        </p>

        <div className="heroActions">
          <input
            ref={inputRef}
            type="file"
            accept=".docx"
            className="hiddenInput"
            onChange={(e) => props.setFile(e.target.files?.[0] || null)}
          />
          <button className="btn btnDark" onClick={() => inputRef.current?.click()}>
            选择 DOCX
          </button>
          <button className="btn btnPrimary" disabled={!props.file || props.isReviewing} onClick={props.onStartReview}>
            {props.isReviewing ? '审查中…' : '开始审查'}
          </button>
          <button className="btn" onClick={props.onOpenHistory}>
            查看记录
          </button>
        </div>

        <div className="heroFileCard">
          <div className="heroFileLabel">当前文件</div>
          <div className="heroFileName">{props.file ? props.file.name : '尚未选择合同文件'}</div>
          <div className="heroFileHint">建议上传标准 DOCX 文件，当前默认走供应商视角与服务合同模板。</div>
        </div>
      </section>

      <section className="dashboardGrid">
        <article className="dashboardCard glassPane">
          <div className="cardHeader">
            <div>
              <div className="cardTitle">上传引导</div>
              <div className="cardHint">为首屏保留一个非常清爽的操作路径。</div>
            </div>
            <span className="statusChip">Step 1</span>
          </div>
          <ul className="featureList">
            <li>先在首页选择 DOCX，再开始审查。</li>
            <li>运行中自动切到结果视图，文档和风险分栏展示。</li>
            <li>完成后会自动积累到“审查记录”。</li>
          </ul>
        </article>

        <article className="dashboardCard glassPane">
          <div className="cardHeader">
            <div>
              <div className="cardTitle">最近一次运行</div>
              <div className="cardHint">随时从首页回到上次的结果页。</div>
            </div>
            <span className="statusChip">Quick Access</span>
          </div>

          {props.latestReview ? (
            <>
              <div className="latestMetaLine">{props.latestReview.file_name || props.latestReview.run_id}</div>
              <div className="latestMetaSub">{props.latestReview.summary || props.latestReview.status}</div>
              <div className="latestMetaSub">更新时间：{new Date(props.latestReview.updated_at).toLocaleString()}</div>
              <button className="btn btnSoft" onClick={props.onOpenLatest}>
                打开当前结果
              </button>
            </>
          ) : (
            <div className="emptyCardState">还没有运行记录，先上传一份合同试试看。</div>
          )}
        </article>
      </section>
    </div>
  )
}
