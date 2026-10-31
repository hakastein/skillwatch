import { StrictMode, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { get, type Tree } from './api'
import { CalibrationPage, FeedPage, MemoryPage, RulesPage, SkillPage, Sidebar } from './pages'
import './styles.css'

// Stable links: #/feed, #/skill/<name>, #/memory/<host:profile>, #/rules, #/calibration[/todo|rated|wrong]
function parseRoute() {
  const [, k = 'feed', ...rest] = location.hash.split('/')
  return { k, arg: decodeURIComponent(rest.join('/')) }
}

function App() {
  const [route, setRoute] = useState(parseRoute)
  const [tree, setTree] = useState<Tree | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [q, setQ] = useState('')
  useEffect(() => {
    const on = () => { setRoute(parseRoute()); document.querySelector('.main')?.scrollTo(0, 0) }
    addEventListener('hashchange', on)
    return () => removeEventListener('hashchange', on)
  }, [])
  useEffect(() => {
    const load = () => get<Tree>('/api/tree').then(setTree, e => setErr(String(e)))
    load()
    const t = setInterval(load, 60_000)
    return () => clearInterval(t)
  }, [])
  if (err && !tree) return <div className="err">The service is unavailable: {err}</div>
  if (!tree) return <div className="err" style={{ color: 'var(--text3)' }}>Loading…</div>
  const page = route.k === 'skill' ? <SkillPage name={route.arg} />
    : route.k === 'memory' ? <MemoryPage k={route.arg} />
    : route.k === 'rules' ? <RulesPage />
    : route.k === 'calibration' ? <CalibrationPage tab={route.arg} /> : <FeedPage />
  return <div className="app"><Sidebar tree={tree} route={route} q={q} setQ={setQ} /><main className="main">{page}</main></div>
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>)
