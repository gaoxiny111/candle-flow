export interface ConfluenceHit {
  name: string
  detail: string
}

export function parseConfluence(s: {
  confluence_detail?: ConfluenceHit[] | string | null
  confluence_hits?: string | null
}): ConfluenceHit[] {
  const raw = s.confluence_detail
  if (Array.isArray(raw)) {
    return raw.filter((x) => x && x.name).map((x) => ({ name: x.name, detail: x.detail || '' }))
  }
  if (typeof raw === 'string' && raw.trim().startsWith('[')) {
    try {
      const parsed = JSON.parse(raw)
      if (Array.isArray(parsed)) {
        return parsed.filter((x) => x && x.name).map((x) => ({ name: x.name, detail: x.detail || '' }))
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
