import { FormEvent, useEffect, useState } from 'react'
import { ArrowUpRight, Check, ChevronLeft, CircleAlert, Code2, Download, FileCode2, Globe2, ImageOff, LoaderCircle, ShieldCheck, Sparkles } from 'lucide-react'

type AuditData = {
  url: string
  final_url: string
  status_code: number
  title: string | null
  meta_description: string | null
  h1_tags: string[]
  robots_txt: { exists: boolean; status_code: number | null; content: string | null }
  sitemap_xml: { exists: boolean; status_code: number | null }
  viewport_meta: { exists: boolean; content: string | null }
  is_https: boolean
  images_missing_alt: number
  images_total: number
  json_ld: unknown[]
  page_weight_bytes: number
  has_sitewide_noindex: boolean
  mixed_content_count: number
  scores: Record<string, number>
  overall_score: number
  grade: string
  issues: { category: string; lost_points: number; title: string; explanation: string }[]
  share_id?: string
  share_url?: string
  og_image_url?: string
}

const API_BASE = import.meta.env.VITE_API_URL ?? ''

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  return `${(bytes / 1024).toFixed(1)} KB`
}

function CheckRow({ label, value, detail }: { label: string; value: boolean; detail?: string }) {
  return <div className="check-row"><span className={`status-dot ${value ? 'is-good' : 'is-muted'}`}><Check size={13} strokeWidth={3} /></span><span>{label}</span>{detail && <small>{detail}</small>}</div>
}

function App() {
  const [url, setUrl] = useState('')
  const [audit, setAudit] = useState<AuditData | null>(null)
  const [error, setError] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [isCopied, setIsCopied] = useState(false)
  const [email, setEmail] = useState('')
  const [isUnlocked, setIsUnlocked] = useState(false)
  const [isSubmittingEmail, setIsSubmittingEmail] = useState(false)

  useEffect(() => {
    const shortId = window.location.pathname.match(/^\/r\/([a-zA-Z0-9_-]+)$/)?.[1]
    if (!shortId) return
    setIsLoading(true)
    fetch(`${API_BASE}/api/reports/${shortId}`)
      .then(async (response) => {
        const body = await response.json()
        if (!response.ok) throw new Error(body.detail ?? 'That shared report could not be found.')
        setAudit(body)
        setIsUnlocked(window.localStorage.getItem(`sitescore-unlocked-${shortId}`) === 'true')
      })
      .catch((requestError) => setError(requestError instanceof Error ? requestError.message : 'That shared report could not be loaded.'))
      .finally(() => setIsLoading(false))
  }, [])

  async function copyShareLink() {
    if (!audit?.share_url) return
    await navigator.clipboard.writeText(audit.share_url)
    setIsCopied(true)
    window.setTimeout(() => setIsCopied(false), 1800)
  }

  async function unlockReport(event: FormEvent) {
    event.preventDefault()
    if (!audit?.share_id || !email.trim()) return
    setIsSubmittingEmail(true)
    try {
      const response = await fetch(`${API_BASE}/api/reports/${audit.share_id}/lead`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: email.trim() }) })
      const body = await response.json()
      if (!response.ok) throw new Error(body.detail ?? 'We could not save your email.')
      setIsUnlocked(true)
      window.localStorage.setItem(`sitescore-unlocked-${audit.share_id}`, 'true')
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'We could not save your email.')
    } finally {
      setIsSubmittingEmail(false)
    }
  }

  async function downloadPdf() {
    if (!audit?.share_id || !email.trim()) return
    const response = await fetch(`${API_BASE}/api/reports/${audit.share_id}/pdf`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: email.trim() }) })
    if (!response.ok) return
    const blob = await response.blob()
    const link = document.createElement('a')
    link.href = URL.createObjectURL(blob)
    link.download = `sitescore-${audit.share_id}.pdf`
    link.click()
    URL.revokeObjectURL(link.href)
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setError('')
    setAudit(null)
    setIsUnlocked(false)
    const candidate = url.trim()
    if (!candidate) {
      setError('Enter a URL to start the crawl.')
      return
    }
    const normalized = /^https?:\/\//i.test(candidate) ? candidate : `https://${candidate}`
    try {
      new URL(normalized)
    } catch {
      setError('That URL looks incomplete. Try something like example.com.')
      return
    }

    setIsLoading(true)
    try {
      const response = await fetch(`${API_BASE}/api/audit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: normalized }),
      })
      const body = await response.json()
      if (!response.ok) throw new Error(body.detail ?? 'The crawl could not be completed.')
      setAudit(body)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Something went wrong while crawling that site.')
    } finally {
      setIsLoading(false)
    }
  }

  return <main>
    <nav className="topbar shell">
      <a className="brand" href="/" aria-label="SiteScore home"><span className="brand-mark"><Sparkles size={16} /></span>Site<span>Score</span></a>
      <span className="nav-note">Technical SEO, made legible</span>
    </nav>

    {!audit ? <section className="hero shell">
      <div className="hero-copy">
        <div className="eyebrow"><span className="eyebrow-line" /> FREE SITE CRAWL <span className="eyebrow-line" /></div>
        <h1>Know what your site is <em>saying.</em></h1>
        <p className="hero-lede">Get a clear read on the technical signals search engines see, before they become someone else&apos;s problem.</p>
        <form className="audit-form" onSubmit={handleSubmit}>
          <div className="input-wrap"><Globe2 size={18} /><input value={url} onChange={(event) => setUrl(event.target.value)} placeholder="yourwebsite.com" aria-label="Website URL" /></div>
          <button type="submit" disabled={isLoading}>{isLoading ? <><LoaderCircle className="spin" size={17} /> Crawling...</> : <>Run free crawl <ArrowUpRight size={17} /></>}</button>
        </form>
        {error && <p className="error-message" role="alert"><CircleAlert size={16} /> {error}</p>}
        <div className="trust-row"><span><Check size={14} /> No signup</span><span><Check size={14} /> Raw signals first</span><span><Check size={14} /> Takes seconds</span></div>
      </div>
      <div className="hero-orbit" aria-hidden="true"><div className="orbit-ring ring-one" /><div className="orbit-ring ring-two" /><div className="orbit-core"><Sparkles size={28} /></div><span className="orbit-label label-one">01 / CRAWL</span><span className="orbit-label label-two">SIGNALS IN</span></div>
      <div className="hero-footer"><span>Built for curious marketers</span><span>Stage 01 <i /> Crawl & inspect</span></div>
    </section> : <section className="results shell">
      <button className="back-button" onClick={() => setAudit(null)}><ChevronLeft size={17} /> New crawl</button>
      <div className="results-heading"><div><div className="eyebrow"><span className="eyebrow-line" /> AUDIT COMPLETE</div><h1>Your site's read.</h1><p>Scored from the signals found at <strong>{audit.final_url}</strong></p></div><div className="result-actions"><div className="score-badge"><strong>{audit.overall_score}</strong><span>/ 100 · Grade {audit.grade}</span></div>{audit.share_url && <button className="share-button" onClick={copyShareLink}>{isCopied ? <Check size={15} /> : <ArrowUpRight size={15} />} {isCopied ? 'Copied' : 'Copy share link'}</button>}</div></div>
      <div className="score-strip">{Object.entries(audit.scores).map(([key, value]) => <div className="score-item" key={key}><div><span>{key.replaceAll('_', ' ')}</span><strong>{value}</strong></div><div className="score-bar"><i style={{ width: `${value}%` }} /></div></div>)}</div>
      {audit.issues.length > 0 && <section className="issues-panel"><div className="panel-heading"><div><div className="eyebrow dark"><span className="eyebrow-line" /> PRIORITY FIXES</div><h2>What to look at first.</h2></div>{isUnlocked ? <button className="pdf-button" onClick={downloadPdf}><Download size={14} /> Download PDF</button> : <span className="issue-count">{audit.issues.length} highest-impact issues</span>}</div>{isUnlocked ? <div className="issue-list">{audit.issues.map((issue, index) => <div className="issue" key={`${issue.title}-${index}`}><span className="issue-number">0{index + 1}</span><div><div className="issue-meta"><span>{issue.category}</span><b>{issue.lost_points} pts at risk</b></div><h3>{issue.title}</h3><p>{issue.explanation}</p></div></div>)}</div> : <form className="email-gate" onSubmit={unlockReport}><div><strong>Unlock the fix list</strong><p>Get the plain-English actions behind this score, plus a downloadable PDF.</p></div><div className="email-gate-form"><input type="email" required value={email} onChange={(event) => setEmail(event.target.value)} placeholder="you@company.com" aria-label="Email address" /><button type="submit" disabled={isSubmittingEmail}>{isSubmittingEmail ? <LoaderCircle className="spin" size={15} /> : <>Unlock report <ArrowUpRight size={15} /></>}</button></div></form>}</section>}
      <div className="signal-grid">
        <article className="signal-card signal-wide"><div className="card-heading"><span className="icon-box"><FileCode2 size={17} /></span><span><h2>Page basics</h2><p>The essentials search engines read first.</p></span></div><div className="data-list"><div><dt>Title tag</dt><dd>{audit.title || <span className="empty">Not found</span>}</dd></div><div><dt>Meta description</dt><dd>{audit.meta_description || <span className="empty">Not found</span>}</dd></div><div><dt>H1 tags <small>({audit.h1_tags.length})</small></dt><dd>{audit.h1_tags.length ? audit.h1_tags.map((heading, index) => <span className="tag" key={`${heading}-${index}`}>{heading}</span>) : <span className="empty">None found</span>}</dd></div></div></article>
        <article className="signal-card"><div className="card-heading"><span className="icon-box blue"><ShieldCheck size={17} /></span><span><h2>Access & trust</h2><p>Can crawlers find and understand it?</p></span></div><div className="check-list"><CheckRow label="HTTPS connection" value={audit.is_https} /><CheckRow label="Viewport meta" value={audit.viewport_meta.exists} detail={audit.viewport_meta.exists ? 'Present' : 'Missing'} /><CheckRow label="robots.txt" value={audit.robots_txt.exists} detail={audit.robots_txt.exists ? 'Found' : 'Missing'} /><CheckRow label="sitemap.xml" value={audit.sitemap_xml.exists} detail={audit.sitemap_xml.exists ? 'Found' : 'Missing'} /></div></article>
        <article className="signal-card"><div className="card-heading"><span className="icon-box orange"><ImageOff size={17} /></span><span><h2>Image coverage</h2><p>Alt text helps every visitor.</p></span></div><div className="metric"><strong>{audit.images_total ? Math.round(((audit.images_total - audit.images_missing_alt) / audit.images_total) * 100) : 100}%</strong><span>with alt text</span></div><div className="metric-sub"><span>{audit.images_missing_alt} missing alt</span><span>{audit.images_total} total images</span></div></article>
        <article className="signal-card"><div className="card-heading"><span className="icon-box purple"><Code2 size={17} /></span><span><h2>Structured data</h2><p>Machine-readable context.</p></span></div><div className="json-status"><span className={audit.json_ld.length ? 'status-dot is-good' : 'status-dot is-muted'}>{audit.json_ld.length ? <Check size={13} /> : <CircleAlert size={13} />}</span><strong>{audit.json_ld.length ? `${audit.json_ld.length} JSON-LD block${audit.json_ld.length > 1 ? 's' : ''} found` : 'No JSON-LD found'}</strong></div>{audit.json_ld.length > 0 && <pre>{JSON.stringify(audit.json_ld[0], null, 2)}</pre>}</article>
        <article className="signal-card crawl-meta"><div className="card-heading"><span className="icon-box green"><Globe2 size={17} /></span><span><h2>Crawl details</h2><p>What was fetched and measured.</p></span></div><div className="meta-pairs"><span><b>Page weight</b>{formatBytes(audit.page_weight_bytes)}</span><span><b>Final URL</b>{audit.final_url}</span><span><b>Requested URL</b>{audit.url}</span></div></article>
      </div>
      <div className="results-footnote"><Sparkles size={14} /> Score and priorities are based on the exact Stage 2 rubric. Detailed recommendations come next.</div>
    </section>}
    <footer className="shell footer"><span>© 2026 SiteScore</span><span>Made by <a href="https://nerdpace.com" target="_blank" rel="noreferrer">NerdPace</a></span></footer>
  </main>
}

export default App
