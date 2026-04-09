import React, { useMemo, useState } from 'react'
import type { ReviewResultPayload, RiskItem } from '../types'

function levelLabel(level: string) {
  if (level === 'high') return '高'
  if (level === 'medium') return '中'
  if (level === 'low') return '低'
  return level
}

function stripRuleCodes(text?: string) {
  return (text || '')
    .replace(/[【\[][^【】\[\]\n]{0,80}_[A-Za-z0-9-]{2,}[】\]]\s*/g, '')
    .replace(/(?:^|\s)(?:RULE|TPL|POLICY|CHECK|REG|MODEL|STD|CLAUSE)_[A-Za-z0-9_-]+(?=\s|$)/g, ' ')
    .replace(/\s{2,}/g, ' ')
    .trim()
}

function presentRiskLabel(r: RiskItem) {
  return stripRuleCodes(r.risk_label || r.dimension || '风险项')
}

export function RiskPanel(props: {
  result: ReviewResultPayload | null
  onLocateRisk: (opts: { anchorText?: string; evidenceText?: string; clauseUids?: string[] }) => void
}) {
  const [levelFilter, setLevelFilter] = useState<'all' | 'high' | 'medium' | 'low'>('all')
  const [keyword, setKeyword] = useState('')

  const risks = props.result?.risk_result_validated?.risk_result?.risk_items || []

  const filtered = useMemo(() => {
    const k = keyword.trim()
    return risks.filter((r) => {
      if (levelFilter !== 'all' && r.risk_level !== levelFilter) return false
      const hay = `${r.dimension} ${presentRiskLabel(r)} ${stripRuleCodes(r.issue)} ${stripRuleCodes(r.basis)} ${stripRuleCodes(r.suggestion)}`
      return !k || hay.includes(k)
    })
  }, [risks, levelFilter, keyword])

  const grouped = useMemo(() => {
    const map = new Map<string, RiskItem[]>()
    for (const r of filtered) {
      const key = r.dimension || '未分类'
      const list = map.get(key) || []
      list.push(r)
      map.set(key, list)
    }
    return Array.from(map.entries()).sort((a, b) => a[0].localeCompare(b[0]))
  }, [filtered])

  const counts = useMemo(() => {
    const c = { all: risks.length, high: 0, medium: 0, low: 0 }
    for (const r of risks) {
      if (r.risk_level === 'high') c.high++
      else if (r.risk_level === 'medium') c.medium++
      else if (r.risk_level === 'low') c.low++
    }
    return c
  }, [risks])

  return (
    <div className="riskRoot">
      <div className="paneHeader paneHeader--risk">
        <div className="paneTitle">风险点</div>
      </div>

      {!props.result ? (
        <div className="emptyState">请先在左侧进入“文件上传”，开始新的合同审查。</div>
      ) : (
        <>
          <div className="riskControls">
            <div className="riskTabs">
              <button className={`tab ${levelFilter === 'all' ? 'tab--active' : ''}`} onClick={() => setLevelFilter('all')}>
                全部 ({counts.all})
              </button>
              <button className={`tab ${levelFilter === 'high' ? 'tab--active' : ''}`} onClick={() => setLevelFilter('high')}>
                高 ({counts.high})
              </button>
              <button className={`tab ${levelFilter === 'medium' ? 'tab--active' : ''}`} onClick={() => setLevelFilter('medium')}>
                中 ({counts.medium})
              </button>
              <button className={`tab ${levelFilter === 'low' ? 'tab--active' : ''}`} onClick={() => setLevelFilter('low')}>
                低 ({counts.low})
              </button>
            </div>
            <input
              className="search"
              placeholder="搜索：风险标签 / 问题 / 建议…"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
            />
          </div>

          <div className="riskList">
            {grouped.map(([dim, items]) => (
              <details key={dim} className="riskGroup" open>
                <summary className="riskGroupTitle">
                  <span>{dim}</span>
                  <span className="riskGroupCount">{items.length}</span>
                </summary>
                <div className="riskCards">
                  {items
                    .slice()
                    .sort((a, b) => String(a.risk_level).localeCompare(String(b.risk_level)))
                    .map((r) => (
                      <div key={r.risk_id} className="riskCard">
                        <div className="riskCardHead">
                          <div className="riskTitle">
                            <span className={`riskBadge riskBadge--${r.risk_level}`}>{levelLabel(String(r.risk_level))}</span>
                            <span className="riskLabel">{presentRiskLabel(r)}</span>
                          </div>
                          <button
                            className="btnSmall"
                            onClick={() =>
                              props.onLocateRisk({
                                anchorText: r.anchor_text,
                                evidenceText: r.evidence_text,
                                clauseUids: r.clause_uids
                              })
                            }
                          >
                            定位原文
                          </button>
                        </div>

                        <div className="riskSection">
                          <div className="riskSectionTitle">问题</div>
                          <div className="riskSectionBody">{stripRuleCodes(r.issue)}</div>
                        </div>
                        <div className="riskSection">
                          <div className="riskSectionTitle">依据</div>
                          <div className="riskSectionBody">{stripRuleCodes(r.basis)}</div>
                        </div>
                        <div className="riskSection">
                          <div className="riskSectionTitle">建议</div>
                          <div className="riskSectionBody">{stripRuleCodes(r.suggestion)}</div>
                        </div>
                      </div>
                    ))}
                </div>
              </details>
            ))}
            {grouped.length === 0 ? <div className="emptyState">当前筛选条件下没有风险项。</div> : null}
          </div>
        </>
      )}
    </div>
  )
}
