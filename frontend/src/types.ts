export type ReviewSideOption = '甲方' | '乙方'

export type ReviewMeta = {
  run_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  file_name?: string
  review_side?: string
  contract_type_hint?: string
  step?: string
  progress?: number
  message?: string
  error?: string
  warning?: string
  updated_at?: string
}

export type Clause = {
  clause_uid: string
  segment_id: string
  segment_title: string
  clause_id: string
  display_clause_id: string
  clause_title?: string
  clause_text: string
  clause_kind?: 'contract_clause' | 'placeholder_clause' | 'note_clause' | 'template_instruction'
  source_excerpt?: string
  numbering_confidence?: number | null
  title_confidence?: number | null
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
  suggestion_minimal?: string
  suggestion_optimized?: string
  evidence_confidence?: number | null
  quality_flags?: string[]
  related_clause_ids?: string[]
  related_clause_uids?: string[]
  clause_id?: string
  anchor_text?: string
  status?: 'pending' | 'accepted' | 'rejected' | string
  clause_uid?: string
  clause_uids?: string[]
  display_clause_ids?: string[]
  is_multi_clause_risk?: boolean
  risk_source_type?: 'anchored' | 'missing_clause' | 'multi_clause'

  /**
   * Legacy AI apply payload (old backend).
   * Kept for backward compatibility.
   */
  ai_apply?: {
    state: string
    edit_type?: string
    target_text?: string
    revised_text?: string
    rationale?: string
    comment_text?: string
    applied_at?: string
  }

  /**
   * New AI rewrite payload (latest backend).
   */
  ai_rewrite?: {
    state: string
    target_text: string
    revised_text: string
    comment_text: string
    created_at: string
  }
  ai_rewrite_decision?: 'proposed' | 'accepted' | 'rejected' | string
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
  tagText?: string
  kind?: 'normal' | 'suggest_insert'
  sourceRiskId?: string
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
