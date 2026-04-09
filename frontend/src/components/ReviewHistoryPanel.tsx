import React, { useEffect, useMemo, useState } from 'react'
import { CalendarDays, ChevronLeft, ChevronRight, FileText, History } from 'lucide-react'
import type { ReviewHistoryItem } from '../types'

function statusLabel(status: ReviewHistoryItem['status']) {
  if (status === 'completed') return '审查完成'
  if (status === 'running') return '审查中'
  if (status === 'queued') return '排队中'
  if (status === 'failed') return '失败'
  return status
}

export function ReviewHistoryPanel(props: {
  items: ReviewHistoryItem[]
  stats: any
  latestReview: ReviewHistoryItem | null
  onOpen: (item: ReviewHistoryItem) => void
  onStartNew: () => void
}) {
  const [pageSize, setPageSize] = useState(10)
  const [page, setPage] = useState(1)
  const totalPages = Math.max(1, Math.ceil(props.items.length / pageSize))

  useEffect(() => {
    setPage((prev) => Math.min(prev, totalPages))
  }, [totalPages])

  useEffect(() => {
    const computePageSize = () => {
      const vh = window.innerHeight
      const reserved = vh < 820 ? 390 : 430
      const rowHeight = vh < 820 ? 58 : 64
      const fitRows = Math.floor((vh - reserved) / rowHeight)
      setPageSize(Math.max(5, Math.min(14, fitRows)))
    }
    computePageSize()
    window.addEventListener('resize', computePageSize)
    return () => window.removeEventListener('resize', computePageSize)
  }, [])

  const pagedItems = useMemo(() => {
    const start = (page - 1) * pageSize
    return props.items.slice(start, start + pageSize)
  }, [props.items, page])

  const pageNumbers = useMemo(() => {
    if (totalPages <= 7) return Array.from({ length: totalPages }, (_, idx) => idx + 1)
    if (page <= 4) return [1, 2, 3, 4, 5, totalPages]
    if (page >= totalPages - 3) return [1, totalPages - 4, totalPages - 3, totalPages - 2, totalPages - 1, totalPages]
    return [1, page - 1, page, page + 1, totalPages]
  }, [page, totalPages])

  const showingStart = props.items.length === 0 ? 0 : (page - 1) * pageSize + 1
  const showingEnd = Math.min(page * pageSize, props.items.length)

  return (
    <div className="historyPage">
      <div className="dashboardScroll h-full flex flex-col">
        <div className="historyHeader mb-5">
          <div className="flex items-start justify-between gap-4">
            <div>
              <div className="flex items-center gap-2.5 mb-2">
                <div className="w-9 h-9 rounded-xl bg-emerald-50 text-emerald-600 flex items-center justify-center border border-emerald-100">
                  <History size={18} />
                </div>
                <h1 className="text-[22px] leading-7 font-semibold text-gray-900">审查记录</h1>
              </div>
              <p className="text-sm text-gray-500">查看并管理历史合同审查任务</p>
            </div>
            <button
              type="button"
              onClick={props.onStartNew}
              className="shrink-0 inline-flex items-center justify-center h-10 px-4 rounded-xl border border-emerald-200 text-emerald-700 text-sm font-medium hover:bg-emerald-50 transition-colors"
            >
              发起新审查
            </button>
          </div>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-4 max-w-[1200px] mx-auto">
          <div className="rounded-2xl border border-gray-100 bg-white px-4 py-3">
            <div className="text-xs text-gray-500 mb-1">总任务数</div>
            <div className="text-xl font-semibold text-gray-900">{props.stats?.total ?? props.items.length}</div>
          </div>
          <div className="rounded-2xl border border-emerald-100 bg-emerald-50/40 px-4 py-3">
            <div className="text-xs text-emerald-700/80 mb-1">已完成</div>
            <div className="text-xl font-semibold text-emerald-700">{props.stats?.completed ?? 0}</div>
          </div>
          <div className="rounded-2xl border border-amber-100 bg-amber-50/40 px-4 py-3">
            <div className="text-xs text-amber-700/80 mb-1">进行中</div>
            <div className="text-xl font-semibold text-amber-700">{props.stats?.running ?? 0}</div>
          </div>
        </div>

        <div className="w-full max-w-[1200px] mx-auto overflow-hidden rounded-2xl border border-gray-100 bg-white shadow-sm flex-1 min-h-0 flex flex-col">
          <table className="historyTable w-full min-w-[680px]">
            <thead>
              <tr>
                <th className="text-left py-3.5 px-6 text-xs font-bold text-gray-500 uppercase tracking-wider">文件名称</th>
                <th className="text-left py-3.5 px-6 text-xs font-bold text-gray-500 uppercase tracking-wider">任务类型</th>
                <th className="text-left py-3.5 px-6 text-xs font-bold text-gray-500 uppercase tracking-wider">审查时间</th>
                <th className="text-left py-3.5 px-6 text-xs font-bold text-gray-500 uppercase tracking-wider">状态</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {props.items.length === 0 ? (
                <tr>
                  <td colSpan={4} className="text-center py-20 text-gray-400">暂无审查记录</td>
                </tr>
              ) : (
                pagedItems.map((item) => (
                  <tr
                    key={item.id}
                    className={`historyRow ${item.available === false ? 'opacity-50 cursor-not-allowed' : 'hover:bg-gray-50 transition-colors cursor-pointer'}`}
                    onClick={item.available === false ? undefined : () => props.onOpen(item)}
                  >
                    <td className="py-3.5 px-6">
                      <div className="flex items-center gap-3">
                        <div className="w-8 h-8 rounded-lg bg-blue-50 flex items-center justify-center">
                          <FileText size={16} className="text-blue-600" />
                        </div>
                        <span className="text-sm font-medium text-gray-900 truncate max-w-[200px]">{item.file_name || item.run_id}</span>
                      </div>
                    </td>
                    <td className="py-3.5 px-6 text-sm text-gray-600">深度审查</td>
                    <td className="py-3.5 px-6 text-sm text-gray-500">
                      <div className="inline-flex items-center gap-1.5">
                        <CalendarDays size={14} className="text-gray-400" />
                        <span>
                          {new Date(item.updated_at).toLocaleString('zh-CN', {
                            month: '2-digit',
                            day: '2-digit',
                            hour: '2-digit',
                            minute: '2-digit'
                          })}
                        </span>
                      </div>
                    </td>
                    <td className="py-3.5 px-6">
                      <div className="flex items-center gap-2">
                        <span className={`statusDot statusDot--${item.available === false ? 'queued' : item.status}`} />
                        <span className="text-sm text-gray-700">{item.available === false ? '缺少文件' : statusLabel(item.status)}</span>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>

          {props.items.length > 0 ? (
            <div className="flex items-center justify-between border-t border-gray-100 px-4 sm:px-6 py-3 bg-gray-50/60">
              <div className="text-xs sm:text-sm text-gray-500">
                显示第 {showingStart}-{showingEnd} 条，共 {props.items.length} 条
              </div>
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={page <= 1}
                  className="h-8 w-8 inline-flex items-center justify-center rounded-lg border border-gray-200 text-gray-600 hover:bg-white disabled:opacity-40 disabled:cursor-not-allowed"
                  aria-label="上一页"
                >
                  <ChevronLeft size={16} />
                </button>
                {pageNumbers.map((num, idx) => {
                  const prev = pageNumbers[idx - 1]
                  const needBreak = typeof prev === 'number' && num - prev > 1
                  return (
                    <React.Fragment key={num}>
                      {needBreak ? <span className="px-1 text-gray-400 text-xs">...</span> : null}
                      <button
                        type="button"
                        onClick={() => setPage(num)}
                        className={`h-8 min-w-8 px-2 inline-flex items-center justify-center rounded-lg border text-sm ${
                          num === page
                            ? 'border-emerald-200 bg-emerald-50 text-emerald-700 font-medium'
                            : 'border-gray-200 text-gray-600 hover:bg-white'
                        }`}
                      >
                        {num}
                      </button>
                    </React.Fragment>
                  )
                })}
                <button
                  type="button"
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  disabled={page >= totalPages}
                  className="h-8 w-8 inline-flex items-center justify-center rounded-lg border border-gray-200 text-gray-600 hover:bg-white disabled:opacity-40 disabled:cursor-not-allowed"
                  aria-label="下一页"
                >
                  <ChevronRight size={16} />
                </button>
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  )
}
