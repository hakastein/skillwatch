import { Fragment, useEffect, useMemo, useState } from 'react'
import { get, type Calibration, type Event, type Feed, type Memory, type Rules, type SkillDetail, type Tree } from './api'
import { ChangeCard, Icon, Markdown, abs, dayKey, plural } from './components'

export const KIND_LABEL: Record<string, string> = { shared: 'Shared', publisher: 'Publisher', project: 'Project', local: 'Local' }

function useData<T>(url: string, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => { setData(null); setErr(null); get<T>(url).then(setData, e => setErr(String(e))) }, [url, ...deps])
  return { data, err }
}

const Err = ({ err }: { err: string }) => <div className="err">Could not load: {err}</div>
const Loading = () => <div className="wrap empty">Loading…</div>

function Timeline({ events, showSkill }: { events: Event[]; showSkill: boolean }) {
  const days: { k: string; evs: Event[] }[] = []
  for (const e of events) {
    const k = dayKey(e.ts)
    if (days.at(-1)?.k !== k) days.push({ k, evs: [] })
    days.at(-1)!.evs.push(e)
  }
  return <>{days.map(d => {
    const nb = d.evs.filter(e => e.verdict === 'reject').length
    const ns = new Set(d.evs.map(e => e.skill ?? `mem:${e.profile}`)).size
    return <div key={d.k}>
      <div className="day">{d.k}<span>{d.evs.length} {plural(d.evs.length, 'edit', 'edits')}
        {showSkill && <> · {ns} {plural(ns, 'object', 'objects')}</>}
        {nb > 0 && <> · <b style={{ color: 'var(--bad)' }}>{nb} rejected</b></>}</span></div>
      {d.evs.map(e => <ChangeCard key={e.key} e={e} showSkill={showSkill} />)}
    </div>
  })}</>
}

export function FeedPage() {
  const [limit, setLimit] = useState(60)
  const { data, err } = useData<Feed>(`/api/feed?limit=${limit}`)
  const [last, setLast] = useState<Feed | null>(null)
  useEffect(() => { if (data) setLast(data) }, [data])
  useEffect(() => {  // live feed: new edits and review progress without a reload; expanded cards stay open
    const t = setInterval(() => get<Feed>(`/api/feed?limit=${limit}`).then(setLast, () => {}), 5000)
    return () => clearInterval(t)
  }, [limit])
  const feed = last ?? data
  if (err) return <Err err={err} />
  if (!feed) return <Loading />
  const s = feed.summary
  return <div className="wrap">
    <h1>Change feed</h1>
    <p className="desc">What Hermes changes in its skills and memory: what, where and who. The reviewer writes the summary and a note; rejected writes were not applied.</p>
    <div className="stats">
      <div className="st"><div className="v">{feed.total}</div><div className="k">edits in total</div></div>
      <div className="st"><div className="v" style={{ color: 'var(--ok)' }}>{s.pass}</div><div className="k">passed by the reviewer</div></div>
      <div className="st"><div className="v" style={{ color: 'var(--bad)' }}>{s.reject}</div><div className="k">rejected</div></div>
      <div className="st"><div className="v" style={{ color: 'var(--text3)' }}>{s.unreviewed}</div><div className="k">unreviewed</div></div>
    </div>
    <Timeline events={feed.events} showSkill />
    {feed.events.length < feed.total && <button className="more" onClick={() => setLimit(limit + 100)}>Show more</button>}
  </div>
}

export function SkillPage({ name }: { name: string }) {
  const { data, err } = useData<SkillDetail>(`/api/skill/${encodeURIComponent(name)}`)
  const [file, setFile] = useState('SKILL.md')
  const [raw, setRaw] = useState(false)
  useEffect(() => { setFile('SKILL.md'); setRaw(false) }, [name])
  if (err) return <Err err={err} />
  if (!data) return <Loading />
  const s = data.copies[0]
  const f = data.files.find(x => x.path === file) ?? data.files[0]
  const fm = f?.path === 'SKILL.md' && f.text ? (f.text.match(/^---\r?\n([\s\S]*?)\r?\n---/)?.[1] ?? '') : ''
  const fmRows = fm.split(/\r?\n/).filter(l => /^[a-z_]+:\s*\S/.test(l)).map(l => [l.slice(0, l.indexOf(':')), l.slice(l.indexOf(':') + 1).trim().replace(/^["']|["']$/g, '')])
  const rejected = data.history.filter(e => e.verdict === 'reject').length
  const scroll = (id: string) => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth' })
  return <div className="wrap">
    <div className="crumb">{s ? `${KIND_LABEL[s.kind]}${s.group ? ' / ' + s.group : ''}` : 'Skill'}</div>
    <h1>{s?.name ?? name}</h1>
    {s ? <p className="desc">{s.description}</p> : <p className="desc">The skill is not on disk now — removed or archived.</p>}
    {s && <div className="meta">
      {s.kind === 'publisher' && <span className="chip lock"><Icon n="lock" />Publisher · Hermes does not edit it</span>}
      {s.profiles.map(p => <span key={p.key} className={`chip ${p.state === 'on' ? 'ok' : 'ghost'}`} title={p.state === 'absent' ? 'The source is not connected to this profile' : undefined}>
        <Icon n={p.state === 'on' ? 'check' : p.state === 'off' ? 'x' : 'dash'} />{p.label}{p.state === 'off' && ' · disabled'}</span>)}
    </div>}
    {s && <div className="path mono">{s.path}</div>}
    {data.copies.length > 1 && <div className="warnline"><Icon n="file" />{data.copies.length - 1} more {plural(data.copies.length - 1, 'copy', 'copies')} with the same name: {data.copies.slice(1).map(c => c.path).join(', ')}</div>}
    <div className="anchors"><a onClick={() => scroll('hist')}>History<span className="n">{data.history.length}</span></a><a onClick={() => scroll('cur')}>Current state</a></div>
    <h2 id="hist">History {rejected > 0 && <span className="chip bad">{rejected} rejected</span>}</h2>
    {data.history.length ? <Timeline events={data.history} showSkill={false} /> : <div className="empty">Hermes has not changed this skill.</div>}
    {f && <>
      <h2 id="cur">Current state</h2>
      <div className="filechips">{data.files.map(x => <button key={x.path} className={`chip ${x.path === f.path ? 'act' : ''}`} onClick={() => setFile(x.path)}><Icon n="file" />{x.path}</button>)}
        {f.path.endsWith('.md') && <button className="chip" style={{ marginLeft: 'auto' }} onClick={() => setRaw(!raw)}>{raw ? 'Rendered' : 'Source'}</button>}</div>
      <div className="doc">
        {fmRows.length > 0 && !raw && <dl className="fm">{fmRows.map(([k, v]) => <Fragment key={k}><dt>{k}</dt><dd>{v}</dd></Fragment>)}</dl>}
        {f.text === null ? <div className="empty">Binary file.</div>
          : f.path.endsWith('.md') && !raw ? <Markdown text={f.text} /> : <pre className="src">{f.text}</pre>}
      </div>
    </>}
  </div>
}

export function MemoryPage({ k }: { k: string }) {
  const { data, err } = useData<Memory>(`/api/memory/${encodeURIComponent(k)}`)
  if (err) return <Err err={err} />
  if (!data) return <Loading />
  return <div className="wrap">
    <div className="crumb">Memory</div>
    <h1>{data.label}</h1>
    <p className="desc">What Hermes injects into every conversation of this profile. Writes here go through the reviewer too.</p>
    <h2>Now</h2>
    {Object.keys(data.files).length === 0 && <div className="empty">The profile has no memory files.</div>}
    {Object.entries(data.files).map(([f, t]) => {
      const items = t.split(/\r?\n§\r?\n/).map(x => x.trim()).filter(Boolean), lim = data.limits[f]
      return <div className="mem" key={f}>
        <div className="hd"><Icon n={f === 'USER.md' ? 'user' : 'mem'} /><b>{f === 'USER.md' ? 'About the user' : 'Agent notes'}</b>
          <span className="mono" style={{ color: 'var(--text3)', fontSize: 12 }}>{f}</span>
          <span className="cnt">{t.length.toLocaleString()} / {lim.toLocaleString()}</span>
          <span className={`bar ${t.length / lim > .9 ? 'full' : ''}`}><i style={{ width: `${Math.min(100, t.length / lim * 100)}%` }} /></span></div>
        {items.map((x, i) => <div className="me" key={i}><span className="k">{i + 1}</span><span>{x}</span></div>)}
      </div>
    })}
    <h2>History</h2>
    {data.history.length ? <Timeline events={data.history} showSkill={false} /> : <div className="empty">No memory writes went through the reviewer yet.</div>}
  </div>
}

export function RulesPage() {
  const { data, err } = useData<Rules>('/api/rules')
  if (err) return <Err err={err} />
  if (!data) return <Loading />
  const total = data.stats.pass + data.stats.reject
  return <div className="wrap">
    <h1>Reviewer rules</h1>
    <p className="desc">The reviewer is a one-shot Hermes run that cannot write. It sees the write, the skill, the profile's skill list and the session's messages, and reads the rest itself. The rules are the <span className="mono">{data.path}</span> file (the <span className="mono">rules</span> key of <span className="mono">reviewer.yaml</span>); change them through an agent in chat.</p>
    <div className="stats">
      <div className="st"><div className="v">{total}</div><div className="k">reviewed</div></div>
      <div className="st"><div className="v" style={{ color: 'var(--ok)' }}>{data.stats.pass}</div><div className="k">passed</div></div>
      <div className="st"><div className="v" style={{ color: 'var(--bad)' }}>{data.stats.reject}</div><div className="k">rejected</div></div>
      <div className="st"><div className="v" style={{ fontSize: 15, paddingTop: 5 }}>{data.review_on ? 'on' : 'off'}</div><div className="k">reviewer</div></div>
    </div>
    {Object.keys(data.stats.by_rule).length > 0 && <div className="meta" style={{ marginBottom: 12 }}>
      {Object.entries(data.stats.by_rule).sort((a, b) => b[1] - a[1]).map(([r, n]) => <span key={r} className="chip bad">{r}: {n}</span>)}</div>}
    <div className="doc"><Markdown text={data.text} /></div>
  </div>
}

export function CalibrationPage({ tab }: { tab: string }) {
  const show = ['rated', 'wrong'].includes(tab) ? tab : 'todo'
  const { data, err } = useData<Calibration>(`/api/calibration?show=${show}`)
  if (err) return <Err err={err} />
  if (!data) return <Loading />
  const cur = data.versions[0]
  const tabs: [string, string, number | null][] = [['todo', 'To rate', data.todo], ['rated', 'Rated', cur ? cur.right + cur.wrong : 0], ['wrong', 'Reviewer wrong', cur?.wrong ?? 0]]
  return <div className="wrap">
    <h1>Reviewer tuning</h1>
    <p className="desc">The reviewer with the current rules runs over a set of real edits. You rate its decisions:
      right or wrong, and what is off in the reasoning. From your ratings an agent in chat edits the rules and starts a new run — a new version.
      You switch the reviewer on when the result is good enough.</p>
    {data.versions.length > 0 && <table className="vt">
      <thead><tr><th>Version</th><th>Model</th><th>Decisions</th><th>Passed</th><th>Rejected</th><th>Errors</th><th>Median / p90</th><th>Same as reference</th><th>Right</th><th>Wrong</th></tr></thead>
      <tbody>{data.versions.map(v => <tr key={v.version} className={v === cur ? 'cur' : ''} title={abs(v.ts)}>
        <td>v{v.version}{v.reference && ' · reference'}</td><td>{v.config.model ? `${v.config.model} ${v.config.reasoning}` : '—'}</td>
        <td>{v.total}</td><td>{v.pass}</td><td>{v.reject}</td><td className={v.errors ? 'bad' : ''}>{v.errors}</td>
        <td>{v.median_s ?? '—'} / {v.p90_s ?? '—'} s</td><td>{v.reference ? '—' : `${v.agree_ref} / ${v.ref_total}`}</td>
        <td>{v.right}</td><td className={v.wrong ? 'bad' : ''}>{v.wrong}</td></tr>)}</tbody>
    </table>}
    <div className="anchors">{tabs.map(([k, t, n]) => <a key={k} href={`#/calibration/${k}`} className={k === show ? 'act' : ''}>{t}{n !== null && <span className="n">{n}</span>}</a>)}</div>
    {data.events.length === 0 && <div className="empty">{!cur ? 'No runs yet.' : show === 'todo' ? 'Every decision of this run is rated.' : 'Empty.'}</div>}
    {data.events.map(e => <ChangeCard key={e.key} e={e} defaultOpen={show === 'todo'} />)}
  </div>
}

export function Sidebar({ tree, route, q, setQ }: { tree: Tree; route: { k: string; arg: string }; q: string; setQ: (s: string) => void }) {
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const sel = route.k === 'skill' ? route.arg : null
  const groups = useMemo(() => {
    const out: Record<string, Record<string, Tree['skills']>> = {}
    const ql = q.toLowerCase()
    for (const s of tree.skills.filter(s => !ql || s.name.toLowerCase().includes(ql) || s.description.toLowerCase().includes(ql)))
      ((out[s.kind] ??= {})[s.group] ??= []).push(s)
    return out
  }, [tree, q])
  return <aside className="side">
    <div className="brand"><span className="dot">S</span>skillwatch</div>
    <label className="search"><Icon n="search" /><input placeholder="Find a skill" value={q} onChange={e => setQ(e.target.value)} /></label>
    <nav className="nav">
      {!q && <>
        <a className={`nl ${route.k === 'feed' ? 'sel' : ''}`} href="#/feed"><Icon n="feed" /><span className="lbl">Change feed</span><span className="n">{tree.feed_total}</span></a>
        <div className="nl" style={{ cursor: 'default' }}><Icon n="mem" /><span className="lbl">Memory</span></div>
        {tree.profiles.map(p => <a key={p.key} className={`nl sub ${route.k === 'memory' && route.arg === p.key ? 'sel' : ''}`} href={`#/memory/${encodeURIComponent(p.key)}`}>
          <span className="lbl">{p.label}</span>{!p.has_memory && <span className="n">—</span>}</a>)}
        <a className={`nl ${route.k === 'rules' ? 'sel' : ''}`} href="#/rules"><Icon n="rules" /><span className="lbl">Reviewer rules</span></a>
        <a className={`nl ${route.k === 'calibration' ? 'sel' : ''}`} href="#/calibration"><Icon n="user" /><span className="lbl">Reviewer tuning</span>
          {tree.calib_todo > 0 && <span className="n">{tree.calib_todo}</span>}</a>
      </>}
      {(['shared', 'publisher', 'project', 'local'] as const).filter(k => groups[k]).map(kind => {
        const gs = Object.entries(groups[kind]).sort(([a], [b]) => a.localeCompare(b))
        return <div key={kind}>
          <div className="sec"><span>{KIND_LABEL[kind]}</span><span>{gs.reduce((n, [, ss]) => n + ss.length, 0)}</span></div>
          {gs.map(([g, ss]) => {
            const key = `${kind}/${g}`, isOpen = !!q || open[key] || ss.some(s => s.name === sel)
            return <div key={key}>
              <button className={`cat ${isOpen ? 'open' : ''}`} onClick={() => setOpen({ ...open, [key]: !isOpen })}>
                <Icon n="chev" cls="chev" /><span className="lbl">{g}</span><span className="n">{ss.length}</span></button>
              {isOpen && ss.map(s => <a key={s.path} className={`sk ${s.name === sel ? 'sel' : ''}`} href={`#/skill/${encodeURIComponent(s.name)}`} title={s.description}>
                <span className="lbl">{s.name}</span><span className="m">{(s.rejected ?? 0) > 0 && <span className="pip" title="Has rejected edits" />}{s.changes || ''}</span></a>)}
            </div>
          })}
        </div>
      })}
    </nav>
  </aside>
}
