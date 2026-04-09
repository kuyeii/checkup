import React from 'react'

export function TopBar(props: {
  file: File | null
  statusText: string
  runId: string | null
  riskCount: number
  isReviewing: boolean
  onGoUpload: () => void
  onGoHistory: () => void
  downloadUrl: string | null
}) {
  return (
    <header className="topBar glassPane">
      <div className="topLeft">
        <div className="brand">
          <div className="brandDot" />
          <div>
            <div className="brandText">审查结果工作区</div>
            <div className="brandSubText">文档、风险和操作聚合在同一视图中</div>
          </div>
        </div>

        <div className="filePill" title={props.file?.name || ''}>
          {props.file ? props.file.name : '未选择合同文件'}
        </div>
      </div>

      <div className="topRight">
        <button className="btn" onClick={props.onGoUpload}>
          文件上传
        </button>
        <button className="btn" onClick={props.onGoHistory}>
          审查记录
        </button>
        {props.downloadUrl ? (
          <a className="btn btnPrimary" href={props.downloadUrl} target="_blank" rel="noreferrer">
            下载带批注 DOCX
          </a>
        ) : null}
        {props.runId ? <span className="statusId">Run: {props.runId}</span> : null}
        <span className="statusText">{props.statusText || (props.isReviewing ? '审查中…' : '等待开始')}</span>
        <span className="summaryPill">{props.riskCount} 个风险点</span>
      </div>
    </header>
  )
}
