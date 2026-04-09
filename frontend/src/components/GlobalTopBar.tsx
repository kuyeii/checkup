import React from 'react'
import { User } from 'lucide-react'

export function GlobalTopBar() {
  return (
    <header className="globalTopBar">
      <div className="topBarUser bg-gray-100/80 rounded-full pr-4 pl-1 py-1">
        <div className="avatar">
          <User size={16} />
        </div>
        <span className="text-sm">18969334760</span>
      </div>
    </header>
  )
}
