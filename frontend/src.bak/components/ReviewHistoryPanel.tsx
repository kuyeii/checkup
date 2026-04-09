import React from 'react'
import type { ReviewHistoryItem } from '../types'

function statusLabel(status: ReviewHistoryItem['status']) {
  if (status === 'completed') return '已完成'
  if (status === 'running') return '审查中'
  if (status === 'queued') return '排队中'
  if (status === 'failed') return '失败'
  return status
}

export function ReviewHistoryPanel(props: {
  items: ReviewHistoryItem[]
  onOpen: (item: ReviewHistoryItem) => void
  onStartNew: () => void
}) {
  return (
    <div className="historyPage">
      <section className="sectionIntro">
        <div>
          <div className="eyebrow">审查记录</div>
          <h1 className="sectionTitle">通过侧栏快速切换历史运行。</h1>
          <p className="sectionCopy">这里展示本次会话中的审查记录。点击任意一条，可以重新打开当时的文档与风险结果。</p>
        </div>
        <button className="btn btnDark" onClick={props.onStartNew}>
          新建审查
        </button>
      </section>

      <div className="historyList">
        {props.items.length === 0 ? (
          <div className="emptyHistory glassPane">暂时还没有审查记录。先去“文件上传”发起一次审查。</div>
        ) : (
          props.items.map((item) => (
            <article key={item.id} className="historyCard glassPane">
              <div className="historyCardTop">
                <div>
                  <div className="historyTitle">{item.file_name || item.run_id}</div>
                  <div className="historyRun">Run: {item.run_id}</div>
                </div>
                <span className={`statusPill statusPill--${item.status}`}>{statusLabel(item.status)}</span>
              </div>

              <div className="historyMetaGrid">
                <div>
                  <div className="historyMetaLabel">摘要</div>
                  <div className="historyMetaValue">{item.summary || '暂无摘要'}</div>
                </div>
                <div>
                  <div className="historyMetaLabel">更新时间</div>
                  <div className="historyMetaValue">{new Date(item.updated_at).toLocaleString()}</div>
                </div>
              </div>

              <div className="historyActions">
                <button className="btn btnSoft" onClick={() => props.onOpen(item)}>
                  打开结果
                </button>
              </div>
            </article>
          ))
        )}
      </div>
    </div>
  )
}
