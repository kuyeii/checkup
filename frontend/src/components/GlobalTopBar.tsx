import React from 'react'
import { ShieldCheck, User } from 'lucide-react'

export function GlobalTopBar(props: { variant?: 'landing' | 'user' }) {
  if (props.variant === 'landing') {
    return (
      <header className="landingTopBar">
        <div className="landingTopBarBrand" aria-label="合同审查">
          <span className="landingTopBarLogo" aria-hidden="true">
            <ShieldCheck size={29} strokeWidth={2.35} />
          </span>
          <span className="landingTopBarTitle">合同审查</span>
        </div>

        <nav className="landingTopBarNav" aria-label="首页导航">
          <button type="button" className="landingTopBarLink">使用指南</button>
          <button type="button" className="landingTopBarLink">常见问题</button>
          <button type="button" className="landingLoginBtn">登录 / 注册</button>
        </nav>
      </header>
    )
  }

  return (
    <header className="globalTopBar">
      <div className="topBarUser topBarUser--pill">
        <div className="avatar">
          <User size={16} />
        </div>
        <span className="topBarUserPhone">18969334760</span>
      </div>
    </header>
  )
}
