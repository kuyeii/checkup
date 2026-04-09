export type ReviewMeta = {
  run_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  file_name?: string
  review_side?: string
  contract_type_hint?: string
  step?: string
  error?: string
  warning?: string
}

export type Clause = {
  clause_uid: string
  segment_id: string
  segment_title: string
  clause_id: string
  display_clause_id: string
  clause_title?: string
  clause_text: string
  clause_kind?: 'contract_clause' | 'template_instruction'
  is_boilerplate_instruction?: boolean
}

export type RiskItem = {
  risk_id: number
  dimension: string
  risk_label: string
  risk_level: 'high' | 'medium' | 'low' | string
  issue: string
  basis: string
  evidence_text?: string
  suggestion: string
  clause_id?: string
  anchor_text?: string
  status?: 'pending' | 'accepted' | 'rejected' | string
  clause_uid?: string
  clause_uids?: string[]
  display_clause_ids?: string[]
}

export type ReviewResultPayload = {
  run_id: string
  status: string
  file_name?: string
  review_side?: string
  contract_type_hint?: string
  merged_clauses: Clause[]
  risk_result_validated: {
    is_valid: boolean
    error_message?: string
    risk_result: {
      risk_items: RiskItem[]
    }
  }
  download_ready: boolean
  download_url?: string | null
}

export type EditSummary = {
  id: string
  blockId: string
  type: 'insert' | 'delete' | 'replace'
  insertedText: string
  deletedText: string
  updatedAt: number
  startIndex: number
  endIndex: number
}

export type ReviewHistoryItem = {
  id: string
  run_id: string
  file_name?: string
  status: ReviewMeta['status']
  summary?: string
  updated_at: string
  created_at: string
  available: boolean
  file?: File | null
  meta?: ReviewMeta | null
  result?: ReviewResultPayload | null
}
