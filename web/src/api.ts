export type ProfileState = { key: string; label: string; state: 'on' | 'off' | 'absent' }

export type Skill = {
  name: string; dir: string; description: string; kind: 'shared' | 'publisher' | 'project' | 'local'
  group: string; path: string; profiles: ProfileState[]; changes?: number; rejected?: number
}

export type Event = {
  key: string; ts: string; kind: 'skill' | 'memory'; skill: string | null; memory_target?: string | null
  profile: string; profile_label: string; actor: 'agent' | 'curator' | null; action: string | null
  session_id: string | null; session_title: string | null; applied: boolean
  status: 'running' | 'done' | 'error' | 'unreviewed'; verdict: 'pass' | 'reject' | null
  what: string | null; where: string | null; note: string | null; rule: string | null
  add: number; rem: number; files: string[]
  label?: { verdict: 'ok' | 'bad' | null; comment: string | null } | null
  calib?: { review_id: number; version: number; status: string; verdict: 'pass' | 'reject' | null; what: string | null
    note: string | null; rule: string | null; feedback: { verdict: 'right' | 'wrong' | null; comment: string | null } | null } | null
}

export type Version = { version: number; ts: string; config: { provider?: string; model?: string; reasoning?: string; context?: number }
  reference: boolean; total: number; pass: number; reject: number; errors: number; median_s: number | null; p90_s: number | null
  agree_ref: number; ref_total: number; right: number; wrong: number }
export type Calibration = { events: Event[]; todo: number; versions: Version[] }

export type FileDiff = { file: string; before: string; after: string; missing?: boolean }

export type Tree = { skills: Skill[]; profiles: { key: string; label: string; has_memory: boolean }[]; feed_total: number; calib_todo: number }
export type Feed = { total: number; events: Event[]; summary: { pass: number; reject: number; unreviewed: number } }
export type SkillDetail = { name: string; copies: Skill[]; files: { path: string; text: string | null }[]; history: Event[] }
export type Memory = { key: string; label: string; files: Record<string, string>; limits: Record<string, number>; history: Event[] }
export type Rules = { text: string; review_on: boolean; stats: { pass: number; reject: number; by_rule: Record<string, number> } }

export async function get<T>(url: string): Promise<T> {
  const r = await fetch(url)
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
  return r.json()
}

export async function put(url: string, body: unknown): Promise<void> {
  const r = await fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
}
