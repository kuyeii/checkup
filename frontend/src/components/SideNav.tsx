import React from 'react'

export type NavKey = 'upload' | 'history' | 'waiting' | 'result'

function NavIcon(props: { kind: NavKey }) {
  const common = { width: 18, height: 18, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const }
  if (props.kind === 'upload') {
    return (
      <svg {...common}>
        <path d="M12 16V5" />
        <path d="m7.5 9.5 4.5-4.5 4.5 4.5" />
        <rect x="4" y="16" width="16" height="4" rx="2" />
      </svg>
    )
  }
  if (props.kind === 'history') {
    return (
      <svg {...common}>
        <path d="M3 12a9 9 0 1 0 3-6.7" />
        <path d="M3 4v5h5" />
        <path d="M12 7v5l3 2" />
      </svg>
    )
  }
  if (props.kind === 'waiting') {
    return (
      <svg {...common}>
        <circle cx="12" cy="12" r="8" />
        <path d="M12 8v5" />
        <path d="M12 12h3" />
      </svg>
    )
  }
  return (
    <svg {...common}>
      <rect x="3.5" y="4" width="17" height="16" rx="3" />
      <path d="M8 9.5h8" />
      <path d="M8 13h8" />
      <path d="M8 16.5h5" />
    </svg>
  )
}

function ToggleIcon(props: { collapsed: boolean }) {
  return props.collapsed ? (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="m9 18 6-6-6-6" />
    </svg>
  ) : (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="m15 18-6-6 6-6" />
    </svg>
  )
}

const NAVS: Array<{ key: NavKey; label: string; desc: string }> = [
  { key: 'upload', label: '文件上传', desc: '开始新的合同审查' },
  { key: 'history', label: '审查记录', desc: '查看历史运行结果' },
  { key: 'waiting', label: '审查进度', desc: '查看当前任务进度' },
  { key: 'result', label: '当前结果', desc: '查看文档与风险对照' }
]

export function SideNav(props: {
  activeNav: NavKey
  onSelect: (key: NavKey) => void
  reviewCount: number
  currentRunId: string | null
  collapsed: boolean
  onToggleCollapsed: () => void
}) {
  return (
    <aside className={`sideNav ${props.collapsed ? 'sideNav--collapsed' : 'sideNav--expanded'}`}>
      <div className="sideNavTop">
        <div className="brandPanel" aria-hidden="true">
          <div className="brandBadge">CR</div>
          {!props.collapsed ? (
            <div className="brandMeta">
              <div className="brandTitle">合同审查</div>
              <div className="brandSubtitle">审查导航</div>
            </div>
          ) : null}
        </div>

        <button
          className="sideToggleBtn"
          onClick={props.onToggleCollapsed}
          aria-label={props.collapsed ? '展开侧边栏' : '收起侧边栏'}
          title={props.collapsed ? '展开侧边栏' : '收起侧边栏'}
        >
          <ToggleIcon collapsed={props.collapsed} />
        </button>
      </div>

      <nav className="navList" aria-label="主导航">
        {NAVS.map((item) => (
          <button
            key={item.key}
            className={`navItem ${props.activeNav === item.key ? 'navItem--active' : ''}`}
            onClick={() => props.onSelect(item.key)}
            title={props.collapsed ? item.label : undefined}
            aria-label={item.label}
          >
            <span className="navIconWrap">
              <NavIcon kind={item.key} />
            </span>
            {!props.collapsed ? (
              <span className="navTextWrap">
                <span className="navLabel">{item.label}</span>
                <span className="navDesc">{item.desc}</span>
              </span>
            ) : null}
          </button>
        ))}
      </nav>

      <div className="sideNavFooter" title={props.currentRunId || `${props.reviewCount} 次审查`}>
        <div className="sideFooterBadge">{props.reviewCount}</div>
        {!props.collapsed ? (
          <div className="sideFooterMeta">
            <div className="sideFooterLabel">当前记录</div>
            <div className="sideFooterValue">{props.currentRunId || '未选择结果'}</div>
          </div>
        ) : null}
      </div>
    </aside>
  )
}
