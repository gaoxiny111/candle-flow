export interface ConfluenceHit {
  name: string
  detail: string
  penalty?: boolean
}

export function parseConfluence(s: {
  confluence_detail?: ConfluenceHit[] | string | null
  confluence_hits?: string | null
}): ConfluenceHit[] {
  const raw = s.confluence_detail
  if (Array.isArray(raw)) {
    return raw
      .filter((x) => x && x.name)
      .map((x) => ({ name: x.name, detail: x.detail || '', penalty: Boolean(x.penalty || (x as { weight?: number }).weight != null && Number((x as { weight?: number }).weight) < 0) }))
  }
  if (typeof raw === 'string' && raw.trim().startsWith('[')) {
    try {
      const parsed = JSON.parse(raw)
      if (Array.isArray(parsed)) {
        return parsed
          .filter((x) => x && x.name)
          .map((x) => ({
            name: x.name,
            detail: x.detail || '',
            penalty: Boolean(x.penalty || (typeof x.weight === 'number' && x.weight < 0)),
          }))
      }
    } catch {
      /* fall through */
    }
  }
  if (!s.confluence_hits) return []
  return s.confluence_hits
    .split(',')
    .map((name) => name.trim())
    .filter(Boolean)
    .map((name) => ({ name, detail: '' }))
}
