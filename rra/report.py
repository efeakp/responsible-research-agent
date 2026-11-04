"""Renders a self-contained HTML report from report.json + audit.jsonl.

Machine verdicts and human decisions are shown side by side; a human
resolution never overwrites what the machine said."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from jinja2 import Environment, select_autoescape

from .audit import verify_chain
from .models import RunReport

LABELS = {
    "verified": ("Verified", "ok"), "metadata_mismatch": ("Metadata mismatch", "warn"),
    "found_by_search": ("Found by search", "warn"), "not_found": ("Not found", "bad"),
    "retracted": ("Retracted", "bad"), "unchecked": ("Unchecked", "muted"),
    "supported": ("Supported", "ok"), "partially_supported": ("Partially supported", "warn"),
    "not_supported": ("Not supported", "bad"), "insufficient_evidence": ("Insufficient evidence", "muted"),
    "needs_human_review": ("Needs human review", "review"),
}

TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Verification report — {{ r.run_id }}</title>
<style>
:root{--bg:#f7f6f2;--card:#fff;--ink:#1d1f21;--sub:#5d6166;--line:#e3e1da;
--ok:#1f7a4d;--okbg:#e4f3ea;--warn:#8a5a00;--warnbg:#fbf0d9;--bad:#a8261c;--badbg:#fbe4e1;
--rev:#3b4fa8;--revbg:#e6e9fa;--mut:#6b6f75;--mutbg:#eeede8}
@media (prefers-color-scheme:dark){:root{--bg:#141517;--card:#1d1f22;--ink:#ecebe7;--sub:#a3a6ab;
--line:#2e3135;--okbg:#17352a;--ok:#7fd3a6;--warnbg:#3a2f14;--warn:#f0c46a;--badbg:#3d1c19;
--bad:#f29b90;--revbg:#22284a;--rev:#a9b6f5;--mutbg:#2a2c2f;--mut:#a3a6ab}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif}
main{max-width:1040px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}h2{font-size:18px;margin:36px 0 12px}
.sub{color:var(--sub);font-size:13px}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:22px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.tile b{display:block;font-size:24px;font-variant-numeric:tabular-nums}.tile span{color:var(--sub);font-size:12.5px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:10px}
.pill{display:inline-block;font-size:12px;font-weight:600;padding:2px 9px;border-radius:999px;white-space:nowrap}
.ok{background:var(--okbg);color:var(--ok)}.warn{background:var(--warnbg);color:var(--warn)}
.bad{background:var(--badbg);color:var(--bad)}.review{background:var(--revbg);color:var(--rev)}.muted{background:var(--mutbg);color:var(--mut)}
.row{display:flex;gap:10px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap}
.key{font-weight:700;color:var(--sub);margin-right:6px}.raw{flex:1 1 420px}
ul.issues{margin:8px 0 0;padding-left:18px;color:var(--sub);font-size:13.5px}
.claim{font-size:15px}.src{border-top:1px dashed var(--line);margin-top:10px;padding-top:10px}
blockquote{margin:6px 0;padding:6px 10px;border-left:3px solid var(--ok);background:var(--okbg);font-size:13.5px;border-radius:0 6px 6px 0}
.why{font-size:13.5px;color:var(--sub)}.human{font-size:13px;margin-top:6px;color:var(--rev)}
.banner{border-radius:10px;padding:12px 16px;margin-top:18px;font-size:14px}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;word-break:break-all}
table{border-collapse:collapse;width:100%;font-size:13.5px}td{padding:4px 8px 4px 0;vertical-align:top;border-top:1px solid var(--line)}
td:first-child{color:var(--sub);white-space:nowrap;width:170px}
</style></head><body><main>
<h1>Citation &amp; claim verification</h1>
<div class="sub">{{ r.manuscript }} · run <code>{{ r.run_id }}</code> · {{ r.finished_at }} · checker: {{ method }}</div>

{% if signoffs %}{% set s = signoffs[-1] %}
<div class="banner {{ 'ok' if s.decision=='approve' else 'bad' if s.decision=='reject' else 'warn' }}">
<b>Human sign-off: {{ s.decision|replace('_',' ') }}</b> by {{ s.reviewer }} at {{ s.ts }}{% if s.note %} — {{ s.note }}{% endif %}</div>
{% else %}
<div class="banner review"><b>Awaiting human sign-off.</b> Machine verdicts below are not a final judgement. Record a decision with <code>rra signoff {{ run_dir }} --reviewer NAME --decision approve|approve_with_changes|reject</code>.</div>
{% endif %}

<div class="tiles">
<div class="tile"><b>{{ r.references|length }}</b><span>references checked</span></div>
<div class="tile"><b>{{ ec['verified'] }}</b><span>verified</span></div>
<div class="tile"><b>{{ ec['not_found'] + ec['retracted'] + ec['metadata_mismatch'] }}</b><span>not found / retracted / mismatched</span></div>
<div class="tile"><b>{{ r.claims|length }}</b><span>cited claims</span></div>
<div class="tile"><b>{{ sc['supported'] }}</b><span>claim–source pairs supported</span></div>
<div class="tile"><b>{{ sc['needs_human_review'] + sc['partially_supported'] + sc['not_supported'] }}</b><span>pairs needing attention</span></div>
</div>

{% if r.unresolved_citations or r.uncited_references %}
<h2>Citation integrity</h2><div class="card">
{% if r.unresolved_citations %}<div><span class="pill bad">Cited but not in reference list</span> [{{ r.unresolved_citations|join('], [') }}]</div>{% endif %}
{% if r.uncited_references %}<div style="margin-top:6px"><span class="pill muted">In reference list but never cited</span> [{{ r.uncited_references|join('], [') }}]</div>{% endif %}
</div>{% endif %}

<h2>1 · Do the references exist?</h2>
{% for e in r.references %}{% set l = labels[e.verdict] %}
<div class="card"><div class="row"><div class="raw"><span class="key">[{{ e.reference.key }}]</span>{{ e.reference.raw }}</div>
<div><span class="pill {{ l[1] }}">{{ l[0] }}</span>{% if e.title_similarity is not none %} <span class="sub">title match {{ '%.0f'|format(e.title_similarity*100) }}%</span>{% endif %}</div></div>
{% if e.record %}<div class="sub" style="margin-top:4px">Matched in {{ e.record.source }}{% if e.record.url %}: <a href="{{ e.record.url }}">{{ e.record.url }}</a>{% endif %}</div>{% endif %}
{% if e.issues %}<ul class="issues">{% for i in e.issues %}<li>{{ i }}</li>{% endfor %}</ul>{% endif %}
{% if resolutions.get('R'+e.reference.key) %}{% set h = resolutions['R'+e.reference.key] %}<div class="human">Human: <b>{{ h.verdict }}</b> ({{ h.reviewer }}){% if h.note %} — {{ h.note }}{% endif %}</div>{% endif %}
</div>{% endfor %}

<h2>2 · Do the sources support the claims?</h2>
<div class="sub" style="margin:-6px 0 12px">Judged against each source's abstract only. "Supported" requires a quote that appears word-for-word in that abstract.</div>
{% for c in r.claims %}
<div class="card"><div class="claim"><span class="key">{{ c.id }}</span>{{ c.text }}</div>
{% for s in support_by_claim.get(c.id, []) %}{% set l = labels[s.verdict] %}
<div class="src"><div class="row"><div><span class="key">source [{{ s.reference_key }}]</span><span class="sub">{{ titles.get(s.reference_key) or '' }}</span></div><span class="pill {{ l[1] }}">{{ l[0] }}</span></div>
{% if s.evidence_quote %}<blockquote>“{{ s.evidence_quote }}”</blockquote>{% endif %}
{% if s.rationale %}<div class="why">{{ s.rationale }}</div>{% endif %}
{% set hk = c.id + '/' + s.reference_key %}{% if resolutions.get(hk) %}{% set h = resolutions[hk] %}<div class="human">Human: <b>{{ h.verdict }}</b> ({{ h.reviewer }}){% if h.note %} — {{ h.note }}{% endif %}</div>{% endif %}
</div>{% else %}<div class="src sub">No checkable source (citation key missing from reference list).</div>{% endfor %}
</div>{% endfor %}

<h2>Audit trail</h2><div class="card"><table>
<tr><td>Manuscript SHA-256</td><td><code>{{ r.manuscript_sha256 }}</code></td></tr>
<tr><td>Tool version</td><td>{{ r.tool_version }}</td></tr>
<tr><td>Logged events</td><td>{{ n_events }} ({{ n_http }} database requests, {{ n_llm }} model calls)</td></tr>
<tr><td>Log integrity</td><td><span class="pill {{ 'ok' if chain_ok else 'bad' }}">{{ 'intact' if chain_ok else 'BROKEN' }}</span> {{ chain_msg }}</td></tr>
<tr><td>Replay</td><td><code>rra verify {{ r.manuscript }} --offline</code> reproduces this run from the response cache.</td></tr>
</table></div>

<h2>Known limitations</h2><div class="card why"><ul style="margin:0;padding-left:18px">
<li>Claim support is judged against abstracts, not full text. A claim drawn from a paper's body may show as insufficient or unsupported.</li>
<li>Numeric citation styles only ([1], [2,3], [4–6]). Author–year styles are not yet parsed.</li>
<li>Title, author and year are parsed heuristically from reference lines; an unusual style can cause a false mismatch. Check the issue text before acting.</li>
<li>A reference that verifies here exists and matches its metadata. That does not mean it is a good source.</li>
</ul></div>
</main></body></html>"""


def render(run_dir: Path) -> Path:
    run_dir = Path(run_dir)
    r = RunReport.model_validate_json((run_dir / "report.json").read_text(encoding="utf-8"))
    events = [json.loads(l) for l in (run_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    chain_ok, chain_msg = verify_chain(run_dir / "audit.jsonl")
    signoffs, resolutions = [], {}
    for ev in events:
        if ev["event"] == "human_signoff":
            signoffs.append({**ev["data"], "ts": ev["ts"]})
        elif ev["event"] == "human_resolution":
            resolutions[ev["data"]["item"]] = ev["data"]
    support_by_claim: dict[str, list] = {}
    for s in r.support:
        support_by_claim.setdefault(s.claim_id, []).append(s)
    titles = {e.reference.key: (e.record.title if e.record else None) for e in r.references}
    method = r.support[0].method if r.support else "n/a"
    env = Environment(autoescape=select_autoescape(["html"]))
    html = env.from_string(TEMPLATE).render(
        r=r, labels=LABELS, ec=Counter({**{k: 0 for k in LABELS}, **Counter(e.verdict.value for e in r.references)}),
        sc=Counter({**{k: 0 for k in LABELS}, **Counter(s.verdict.value for s in r.support)}),
        support_by_claim=support_by_claim, titles=titles, signoffs=signoffs,
        resolutions=resolutions, chain_ok=chain_ok, chain_msg=chain_msg, method=method,
        n_events=len(events), n_http=sum(e["event"] == "http_get" for e in events),
        n_llm=sum(e["event"] == "llm_call" for e in events), run_dir=str(run_dir))
    out = run_dir / "report.html"
    out.write_text(html, encoding="utf-8")
    return out
