import React from 'react'
import { Clock, HelpCircle, History, LayoutGrid, Settings, ShieldCheck } from 'lucide-react'
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
  if (status === 'completed') return 'sideNavStatusDot--completed'
  if (status === 'running') return 'sideNavStatusDot--running'
  if (status === 'queued') return 'sideNavStatusDot--queued'
  if (status === 'failed') return 'sideNavStatusDot--failed'
  return 'sideNavStatusDot--queued'
}

function statusText(status?: string) {
  if (status === 'completed') return '已完成'
  if (status === 'running') return '审查中'
  if (status === 'queued') return '排队中'
  if (status === 'failed') return '失败'
  return status || ''
}

function MainNavButton(props: {
  active: boolean
  icon: React.ReactNode
  label: string
  onClick: () => void
}) {
  return (
    <button
      className={`sideNavMainButton ${props.active ? 'sideNavMainButton--active' : ''}`}
      onClick={props.onClick}
      type="button"
    >
      <span className="sideNavMainIcon">{props.icon}</span>
      <span>{props.label}</span>
    </button>
  )
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
    <aside className="sideNav sideNav--landingUnified">
      <div className="sideNavGlow sideNavGlow--top" aria-hidden="true" />
      <div className="sideNavGlow sideNavGlow--bottom" aria-hidden="true" />

      <div className="sideNavTop sideNavTop--landing">
        <div className="sideNavBrand">
          <div className="sideNavBrandMark">
            <ShieldCheck size={22} strokeWidth={2.6} />
          </div>
          <div className="sideNavBrandMeta">
            <span className="sideNavBrandTitle">CHECKUP</span>
            <span className="sideNavBrandSubtitle">合同审查工作台</span>
          </div>
        </div>
      </div>

      <nav className="sideNavPrimaryNav">
        <div className="sideNavSectionTitle">工作台</div>
        <div className="sideNavMainList">
          <MainNavButton
            active={props.activeNav === 'upload'}
            icon={<LayoutGrid size={18} />}
            label="开始审查"
            onClick={() => props.onSelect('upload')}
          />
          <MainNavButton
            active={props.activeNav === 'history'}
            icon={<History size={18} />}
            label="审查记录"
            onClick={() => props.onSelect('history')}
          />
        </div>
      </nav>

      <div className="sideNavRecentScroll">
        <div className="sideNavSectionHeader">
          <span>最近动态</span>
          <span className="sideNavCountPill">{recent.length}</span>
        </div>

        <div className="sideNavRecentPanel">
          {recent.length === 0 ? (
            <div className="sideNavEmptyState">暂无动态</div>
          ) : (
            recent.slice(0, 8).map((it) => {
              const isActive = (props.activeNav === 'result' || props.activeNav === 'waiting') && props.activeRunId && String(props.activeRunId) === String(it.run_id)
              const statusClass = statusDotClass(it.status)
              return (
                <button
                  key={it.run_id}
                  className={`sideNavRecentItem ${isActive ? 'sideNavRecentItem--active' : ''} ${it.available === false ? 'sideNavRecentItem--disabled' : ''}`}
                  onClick={() => props.onOpenRecent && props.onOpenRecent(it)}
                  disabled={!props.onOpenRecent || it.available === false}
                  title={it.available === false ? '缺少原始合同文件，无法打开' : (it.file_name || it.run_id)}
                  type="button"
                >
                  <span className="sideNavRecentIcon">
                    <Clock size={14} />
                    <span className={`sideNavStatusDot ${statusClass}`} />
                  </span>
                  <span className="sideNavRecentText">
                    <span className="sideNavRecentTitle">{it.file_name || it.run_id}</span>
                    <span className="sideNavRecentMeta">
                      <span>{formatRelativeTime(it.updated_at) || ''}</span>
                      <span className="sideNavMetaDivider" />
                      <span className="sideNavStatusText">{statusText(it.status)}</span>
                    </span>
                  </span>
                </button>
              )
            })
          )}
        </div>
      </div>

      <div className="sideNavBottom">
        <nav className="sideNavUtilityList">
          <button className="sideNavUtilityButton" type="button">
            <HelpCircle size={16} />
            <span>帮助中心</span>
          </button>
          <button className="sideNavUtilityButton" type="button">
            <Settings size={16} />
            <span>系统设置</span>
          </button>
        </nav>
      </div>
    </aside>
  )
}
