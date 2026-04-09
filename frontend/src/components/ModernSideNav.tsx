import React from 'react'
import { LayoutGrid, History, Clock, HelpCircle, Settings } from 'lucide-react'
import type { NavKey } from './SideNav'
import type { ReviewHistoryItem } from '../types'

function formatRelativeTime(iso?: string) {
  if (!iso) return ''
  const ts = new Date(iso).getTime()
  if (!Number.isFinite(ts)) return ''
  const diff = Date.now() - ts
  const sec = Math.floor(diff / 1000)
  if (sec < 60) return '刚刚'
  const min = Math.floor(sec / 60)
  if (min < 60) return `${min}分钟前`
  const hr = Math.floor(min / 60)
  if (hr < 24) return `${hr}小时前`
  const day = Math.floor(hr / 24)
  if (day < 7) return `${day}天前`
  return new Date(iso).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function statusDotClass(status?: string) {
  if (status === 'completed') return 'bg-green-500'
  if (status === 'running') return 'bg-yellow-500'
  if (status === 'queued') return 'bg-gray-400'
  if (status === 'failed') return 'bg-red-500'
  return 'bg-gray-300'
}

function statusText(status?: string) {
  if (status === 'completed') return '已完成'
  if (status === 'running') return '审查中'
  if (status === 'queued') return '排队中'
  if (status === 'failed') return '失败'
  return status || ''
}

export function ModernSideNav(props: {
  activeNav: NavKey
  onSelect: (key: NavKey) => void
  recentItems?: ReviewHistoryItem[]
  activeRunId?: string | null
  onOpenRecent?: (item: ReviewHistoryItem) => void
}) {
  const recent = props.recentItems || []
  return (
    <aside className="sideNav flex flex-col h-full pt-3 md:pt-4">
      <div className="sideNavTop shrink-0 px-5 mb-2">
        <div className="flex items-center">
          <div className="w-8 h-8 bg-[#00b365] text-white rounded-full flex items-center justify-center font-bold mr-3 shadow-sm">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-5 h-5"><path d="M2 12h4l2-3 4 6 2-3h4"/></svg>
          </div>
          <span className="font-bold text-xl tracking-wide text-gray-800">CHECKUP</span>
        </div>
      </div>

      {/* Main Navigation (工作台) */}
      <nav className="shrink-0 px-4 space-y-1 mb-3">
        <div className="px-3 mb-2 text-xs font-semibold text-gray-400 uppercase tracking-wider">工作台</div>
        <button
          className={`flex items-center px-4 h-11 w-full text-left rounded-xl transition-all ${props.activeNav === 'upload' ? 'bg-[#f3f4f6] text-gray-900 font-medium' : 'text-gray-600 hover:bg-gray-50'}`}
          onClick={() => props.onSelect('upload')}
        >
          <LayoutGrid size={18} className={`mr-3 ${props.activeNav === 'upload' ? 'text-gray-700' : 'text-gray-400'}`} />
          <span>开始审查</span>
        </button>

        <button
          className={`flex items-center px-4 h-11 w-full text-left rounded-xl transition-all ${props.activeNav === 'history' ? 'bg-[#f3f4f6] text-gray-900 font-medium' : 'text-gray-600 hover:bg-gray-50'}`}
          onClick={() => props.onSelect('history')}
        >
          <History size={18} className={`mr-3 ${props.activeNav === 'history' ? 'text-gray-700' : 'text-gray-400'}`} />
          <span>审查记录</span>
        </button>
      </nav>

      {/* Recent Activity */}
      <div className="flex-1 overflow-y-auto px-4 min-h-0">
        <div className="space-y-3">
          <div className="px-3 text-xs font-semibold text-gray-400 uppercase tracking-wider flex items-center justify-between">
            <span>最近动态</span>
            <span className="text-[11px] font-medium text-gray-400">{recent.length}</span>
          </div>

          <div className="px-3 py-3 rounded-xl border border-gray-100 bg-gray-50/70 space-y-1.5">
            {recent.length === 0 ? (
              <div className="text-xs text-gray-400 py-2 px-1">暂无动态</div>
            ) : (
              recent.slice(0, 8).map((it) => {
                const isActive = (props.activeNav === 'result' || props.activeNav === 'waiting') && props.activeRunId && String(props.activeRunId) === String(it.run_id)
                return (
                  <button
                    key={it.run_id}
                    className={`w-full flex items-start gap-2.5 px-2.5 py-2 rounded-lg transition-colors text-left ${it.available === false ? 'opacity-50 cursor-not-allowed' : ''} ${isActive ? 'bg-white shadow-sm border border-gray-100' : 'hover:bg-white/80'}`}
                    onClick={() => props.onOpenRecent && props.onOpenRecent(it)}
                    disabled={!props.onOpenRecent || it.available === false}
                    title={it.available === false ? '缺少原始合同文件，无法打开' : (it.file_name || it.run_id)}
                  >
                    <div className="mt-0.5 relative">
                      <Clock size={14} className="text-gray-400" />
                      <span className={`absolute -right-1 -bottom-1 w-2 h-2 rounded-full ${statusDotClass(it.status)}`} />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="text-[13px] text-gray-700 font-medium truncate">{it.file_name || it.run_id}</div>
                      <div className="text-[11px] text-gray-400 mt-1 flex items-center gap-2">
                        <span>{formatRelativeTime(it.updated_at) || ''}</span>
                        <span className={`inline-block w-1 h-1 rounded-full ${statusDotClass(it.status)}`} />
                        <span className="truncate max-w-[120px]">{statusText(it.status)}</span>
                      </div>
                    </div>
                  </button>
                )
              })
            )}
          </div>
        </div>
      </div>

      {/* Bottom */}
      <div className="shrink-0 p-4 border-t border-gray-100 mt-auto">
        <nav className="space-y-1.5">
          <button className="flex items-center px-3.5 h-10 w-full text-left rounded-lg text-gray-500 hover:bg-gray-50 transition-colors text-sm">
            <HelpCircle size={16} className="mr-3 text-gray-400" />
            <span>帮助中心</span>
          </button>
          <button className="flex items-center px-3.5 h-10 w-full text-left rounded-lg text-gray-500 hover:bg-gray-50 transition-colors text-sm">
            <Settings size={16} className="mr-3 text-gray-400" />
            <span>系统设置</span>
          </button>
        </nav>
      </div>
    </aside>
  )
}
