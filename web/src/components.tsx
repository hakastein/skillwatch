import { useEffect, useRef, useState } from 'react'
import { DiffEditor, loader } from '@monaco-editor/react'
import * as monaco from 'monaco-editor'
import editorWorker from 'monaco-editor/editor/editor.worker?worker'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { get, put, type Event, type FileDiff } from './api'

// Monaco from the bundle, no CDN.
self.MonacoEnvironment = { getWorker: () => new editorWorker() }
loader.config({ monaco })

const ICONS: Record<string, string> = {
  search: '<circle cx="7" cy="7" r="4.5"/><path d="m13.5 13.5-3.2-3.2"/>',
  feed: '<path d="M2 8h2.5l2-5 3 10 2-5H14"/>',
  mem: '<path d="M4 2.5h6.5L13 5v8.5H4z"/><path d="M6.5 7h4M6.5 9.5h4"/>',
  rules: '<path d="M8 1.8 13 4v4c0 3-2.2 5.2-5 6.2C5.2 13.2 3 11 3 8V4z"/><path d="m5.8 8 1.6 1.6L10.5 6.5"/>',
  chev: '<path d="m6 3.5 4.5 4.5L6 12.5"/>',
  bot: '<rect x="3" y="5" width="10" height="8" rx="2"/><path d="M8 2.5V5M6 9h.01M10 9h.01"/>',
  chat: '<path d="M3 3.5h10v7H7l-3 2.5v-2.5H3z"/>',
  user: '<circle cx="8" cy="5.5" r="2.5"/><path d="M3 13.5c.8-2.5 2.7-3.8 5-3.8s4.2 1.3 5 3.8"/>',
  lock: '<rect x="3.5" y="7" width="9" height="6.5" rx="1.5"/><path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2"/>',
  file: '<path d="M4 1.8h5.5L12 4.3v9.9H4z"/><path d="M9.5 1.8v2.5H12"/>',
  check: '<path d="m3.5 8.5 3 3 6-7"/>', x: '<path d="m4 4 8 8M12 4l-8 8"/>', dash: '<path d="M4 8h8"/>',
  rev: '<circle cx="7" cy="7" r="4.5"/><path d="m13.5 13.5-3.2-3.2M5 7l1.5 1.5L9.2 5.6"/>',
}
export const Icon = ({ n, cls = '' }: { n: string; cls?: string }) =>
  <svg className={`i ${cls}`} viewBox="0 0 16 16" dangerouslySetInnerHTML={{ __html: ICONS[n] }} />

export const plural = (n: number, one: string, many: string) => n === 1 ? one : many
export function rel(ts: string) {
  const d = new Date(ts), m = Math.round((Date.now() - +d) / 60000)
  if (m < 60) return `${Math.max(m, 1)} min ago`
  if (m < 1440) return `${Math.round(m / 60)} h ago`
  const days = Math.round(m / 1440)
  return days < 7 ? `${days} ${plural(days, 'day', 'days')} ago` : d.toLocaleDateString(undefined, { day: 'numeric', month: 'long' })
}
export const time = (ts: string) => new Date(ts).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
export const abs = (ts: string) => new Date(ts).toLocaleString(undefined, { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' })

export function Markdown({ text }: { text: string }) {
  const html = DOMPurify.sanitize(marked.parse(text.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, ''), { async: false }) as string)
  return <div className="md" dangerouslySetInnerHTML={{ __html: html }} />
}

const dark = () => window.matchMedia('(prefers-color-scheme: dark)').matches

const MAX_DIFF = 720

function Diff({ d }: { d: FileDiff }) {
  const [height, setHeight] = useState(MAX_DIFF)
  const box = useRef<HTMLDivElement>(null)
  // Height by what is visible: Monaco folds unchanged regions, and getContentHeight does not subtract them.
  // The box starts at the maximum; after every render we measure lines and zones and shrink it.
  useEffect(() => {
    const el = box.current
    if (!el) return
    let timer = 0
    const measure = () => {
      const lines = el.querySelector('.editor.modified .lines-content')
      if (!lines) return
      const top = lines.getBoundingClientRect().top
      let bottom = 0
      lines.querySelectorAll('.view-line, .view-zones > div').forEach(n => {
        const r = n.getBoundingClientRect()
        if (r.height) bottom = Math.max(bottom, r.bottom - top)
      })
      if (bottom) setHeight(Math.min(MAX_DIFF, Math.ceil(bottom) + 8))
    }
    const obs = new MutationObserver(() => { clearTimeout(timer); timer = window.setTimeout(measure, 60) })
    obs.observe(el, { childList: true, subtree: true, attributes: true, attributeFilter: ['style'] })
    return () => { obs.disconnect(); clearTimeout(timer) }
  }, [])
  const lang = d.file.endsWith('.md') ? 'markdown' : d.file.endsWith('.py') ? 'python' : d.file.endsWith('.sh') ? 'shell' : 'plaintext'
  return <div className="df">
    <div className="fh"><Icon n="file" /><span className="mono">{d.file}</span>{d.missing && <span className="warn">part of the content is unavailable</span>}</div>
    <div style={{ height }} ref={box}>
      <DiffEditor original={d.before} modified={d.after} language={lang} theme={dark() ? 'vs-dark' : 'vs'}
        options={{ readOnly: true, renderSideBySide: false, wordWrap: 'on', diffWordWrap: 'on', minimap: { enabled: false },
          hideUnchangedRegions: { enabled: true, contextLineCount: 2, minimumLineCount: 4 }, scrollBeyondLastLine: false,
          renderOverviewRuler: false, lineNumbersMinChars: 4, fontSize: 12.5, automaticLayout: true,
          scrollbar: { alwaysConsumeMouseWheel: false }, stickyScroll: { enabled: false } }} />
    </div>
  </div>
}

function Diffs({ k }: { k: string }) {
  const [diffs, setDiffs] = useState<FileDiff[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => { get<FileDiff[]>(`/api/change/${encodeURIComponent(k)}`).then(setDiffs, e => setErr(String(e))) }, [k])
  if (err) return <div className="loading">Could not load the diff: {err}</div>
  if (!diffs) return <div className="loading">Loading the diff…</div>
  if (!diffs.length) return <div className="loading">The ledger has no content for this edit.</div>
  return <div className="diffs">{diffs.map((d, i) => <Diff key={i} d={d} />)}</div>
}

const vcls = (e: Event) => e.verdict === 'pass' ? 'ok' : e.verdict === 'reject' ? 'bad' : e.status === 'running' ? 'run' : ''

function Who({ e }: { e: Event }) {
  const actor = e.actor === 'curator' ? ['bot', 'Background learning'] : e.actor === 'agent' ? ['chat', 'Agent in chat'] : ['bot', 'Hermes']
  return <span className="w" title={e.session_title ? `Session: ${e.session_title}` : undefined}>
    <Icon n={actor[0]} />{actor[1]}{e.session_title && <span className="sessn">· {e.session_title}</span>}
  </span>
}

function Verdict({ e }: { e: Event }) {
  if (e.verdict === 'pass' && !e.applied && e.kind === 'skill') return <span className="chip ghost"><Icon n="check" />Passed · never reached the ledger</span>
  if (e.verdict === 'pass') return <span className="chip ok"><Icon n="check" />Passed</span>
  if (e.verdict === 'reject') return <span className="chip bad"><Icon n="x" />Rejected · not applied</span>
  if (e.status === 'running') return <span className="chip">Reviewer is checking…</span>
  return <span className="chip ghost">Unreviewed</span>
}

function What({ e }: { e: Event }) {
  if (e.what) return <div className="what">{e.what}</div>
  if (e.status === 'running') return <div className="what none">The reviewer is writing a summary…</div>
  return <div className="what none">No summary yet — it appears after the background pass</div>
}

type Label = NonNullable<Event['label']>

/** The user's rating of an edit. Stored only in the window's database; never touches Hermes or skills. */
function LabelBox({ e, label, onChange }: { e: Event; label: Label | null; onChange: (l: Label) => void }) {
  const [comment, setComment] = useState(label?.comment ?? '')
  const [msg, setMsg] = useState<{ t: string; err?: boolean } | null>(null)
  const save = (l: Label) => {
    setMsg({ t: 'saving…' })
    put(`/api/label/${encodeURIComponent(e.key)}`, l).then(() => { onChange(l); setMsg({ t: 'saved' }) },
      (err: unknown) => setMsg({ t: `not saved: ${err}`, err: true }))
  }
  const verdict = label?.verdict ?? null
  const pick = (v: 'ok' | 'bad') => save({ verdict: verdict === v ? null : v, comment: comment || null })
  return <div className="lbx">
    <div className="who"><Icon n="user" />Your rating{msg && <span className={`sv ${msg.err ? 'err' : ''}`}>{msg.t}</span>}</div>
    <div className="lbrow">
      <button className={`chip ${verdict === 'ok' ? 'ok' : ''}`} onClick={() => pick('ok')}><Icon n="check" />Fine</button>
      <button className={`chip ${verdict === 'bad' ? 'bad' : ''}`} onClick={() => pick('bad')}><Icon n="x" />Junk</button>
    </div>
    <textarea placeholder="Why — the reviewer prompt is built from this" value={comment} rows={2}
      onChange={ev => { setComment(ev.target.value); setMsg(null) }}
      onBlur={() => comment !== (label?.comment ?? '') && save({ verdict, comment: comment || null })} />
  </div>
}

type Feedback = NonNullable<NonNullable<Event['calib']>['feedback']>

/** The reviewer's decision in the latest calibration run, and your rating of that decision. */
function CalibBox({ e }: { e: Event }) {
  const c = e.calib!
  const [fb, setFb] = useState<Feedback | null>(c.feedback)
  const [comment, setComment] = useState(c.feedback?.comment ?? '')
  const [msg, setMsg] = useState<{ t: string; err?: boolean } | null>(null)
  const save = (f: Feedback) => {
    setMsg({ t: 'saving…' })
    put(`/api/feedback/${c.review_id}`, f).then(() => { setFb(f); setMsg({ t: 'saved' }) },
      (err: unknown) => setMsg({ t: `not saved: ${err}`, err: true }))
  }
  const verdict = fb?.verdict ?? null
  const pick = (v: 'right' | 'wrong') => save({ verdict: verdict === v ? null : v, comment: comment || null })
  const decision = c.status === 'running' ? 'is checking…' : c.status !== 'done' ? 'failed' : c.verdict === 'pass' ? 'would pass' : 'would reject'
  return <div className={`note ${c.status !== 'done' ? '' : c.verdict === 'pass' ? 'ok' : 'bad'}`}>
    <div className="who"><Icon n="rev" />Reviewer v{c.version} {decision}{c.rule ? ` · ${c.rule}` : ''}
      {msg && <span className={`sv ${msg.err ? 'err' : ''}`}>{msg.t}</span>}</div>
    {c.note}
    {c.status === 'done' && <>
      <div className="lbrow" style={{ marginTop: 10 }}>
        <button className={`chip ${verdict === 'right' ? 'ok' : ''}`} onClick={() => pick('right')}><Icon n="check" />Reviewer is right</button>
        <button className={`chip ${verdict === 'wrong' ? 'bad' : ''}`} onClick={() => pick('wrong')}><Icon n="x" />Reviewer is wrong</button>
      </div>
      <textarea placeholder="What is wrong with the decision or the reasoning" value={comment} rows={2}
        onChange={ev => { setComment(ev.target.value); setMsg(null) }}
        onBlur={() => comment !== (fb?.comment ?? '') && save({ verdict, comment: comment || null })} />
    </>}
  </div>
}

const ratable = (e: Event) => e.kind === 'skill' && !e.key.startsWith('review|')

/** One event: what, where, who; on click — the reviewer decision, the diff and a rating. */
export function ChangeCard({ e, showSkill = true, defaultOpen = false }: { e: Event; showSkill?: boolean; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  const [label, setLabel] = useState<Label | null>(e.label ?? null)
  const ref = useRef<HTMLDetailsElement>(null)
  const where = e.where || e.files.join(', ') || (e.kind === 'memory' ? (e.memory_target === 'user' ? 'USER.md' : 'MEMORY.md') : '')
  const target = e.kind === 'memory'
    ? <a className="sklink" href={`#/memory/${encodeURIComponent(e.profile)}`} onClick={ev => ev.stopPropagation()}>Memory · {e.profile_label}</a>
    : showSkill && e.skill ? <a className="sklink" href={`#/skill/${encodeURIComponent(e.skill.split('/').pop()!)}`} onClick={ev => ev.stopPropagation()}>{e.skill}</a> : null
  return <details ref={ref} open={defaultOpen} className={`fe ${e.verdict === 'reject' ? 'bad' : ''}`} onToggle={() => setOpen(!!ref.current?.open)}>
    <summary>
      <span className={`vd ${vcls(e)}`} />
      <div style={{ minWidth: 0 }}>
        <What e={e} />
        <div className="w3">
          <span className="w"><span className="lab">Where</span>{target}{target && where && <span className="sep">›</span>}{where}</span>
          <span className="w"><span className="lab">Who</span><Who e={e} /></span>
          <span className="w"><span className="lab">Profile</span>{e.profile_label}</span>
        </div>
      </div>
      <div className="fe-r"><span className="t" title={abs(e.ts)}>{showSkill ? time(e.ts) : rel(e.ts)}</span>
        {(e.verdict === 'reject' || !showSkill) && <Verdict e={e} />}
        {label?.verdict && <span className={`chip ${label.verdict === 'ok' ? 'ok' : 'bad'}`} title={label.comment ?? undefined}>
          <Icon n="user" />{label.verdict === 'ok' ? 'fine' : 'junk'}</span>}
        {e.calib?.status === 'done' && <span className={`chip ${e.calib.verdict === 'pass' ? 'ok' : 'bad'}`}>
          <Icon n="rev" />v{e.calib.version}: {e.calib.verdict === 'pass' ? 'passed' : 'rejected'}</span>}
        <span className="stat"><span className="a">+{e.add}</span><span className="d">−{e.rem}</span></span></div>
    </summary>
    {(e.note || e.verdict) && <div className={`note ${vcls(e)}`}>
      <div className="who"><Icon n="rev" />{e.status === 'error' ? 'Reviewer failed' : `Reviewer${e.rule ? ` · ${e.rule}` : ''}`}</div>{e.note}</div>}
    {open && e.calib && <CalibBox e={e} />}
    {open && <Diffs k={e.key} />}
    {open && ratable(e) && !e.calib && <LabelBox e={e} label={label} onChange={setLabel} />}
  </details>
}

export function dayKey(ts: string) {
  const d = new Date(ts), today = new Date(new Date().toDateString())
  const diff = Math.round((+today - +new Date(d.toDateString())) / 864e5)
  return diff === 0 ? 'Today' : diff === 1 ? 'Yesterday' : d.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' })
}
