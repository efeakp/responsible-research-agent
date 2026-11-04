import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  isActive,
  isSignedOff,
  type AuditEvent,
  type Claim,
  type Decision,
  type Existence,
  type HumanResolution,
  type RunDetail,
  type Support,
} from "./api";
import { statusInfo, when } from "./format";
import { DECISION_LABEL, REFERENCE_VERDICTS, SUPPORT_VERDICTS, verdictInfo } from "./verdicts";

type Draft = Record<string, { verdict: string; note: string }>;

export default function RunPage({ id }: { id: string }) {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await api.run(id);
      setRun(r);
      if (r.status !== "queued") {
        const a = await api.audit(id).catch(() => null);
        if (a) setEvents(a.events);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    if (!run || !isActive(run.status)) return;
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, [run, load]);

  if (error) return <main className="page"><p className="err">{error}</p></main>;
  if (!run) return <main className="page"><p className="small">Loading…</p></main>;

  const s = statusInfo(run.status);
  return (
    <main className="page">
      <div className="run-head">
        <div>
          <h1>{run.filename}</h1>
          <div className="meta">
            <span className={`status ${s.tone} ${s.live ? "live" : ""}`}>{s.label}</span>
            <span>Started {when(run.created_at)}</span>
            <span>{run.mode === "ollama" ? `Claims checked by ${run.model}` : "Claims checked without a model"}</span>
          </div>
          {run.source && (
            <p className="meta source-line">
              Converted from <a href={api.sourceUrl(run.run_id)}>{run.source.filename}</a> with {run.source.extractor}
              {run.source.edited ? ", then corrected by hand before checking" : ""}.{" "}
              <a href={api.manuscriptUrl(run.run_id)} target="_blank" rel="noreferrer">See the text that was checked</a>
            </p>
          )}
        </div>
        {(run.status === "awaiting_human_signoff" || isSignedOff(run.status)) && (
          <a className="btn ghost" href={api.reportUrl(run.run_id)} target="_blank" rel="noreferrer">
            Open printable report
          </a>
        )}
      </div>

      {isActive(run.status) && <Progress events={events} queued={run.status === "queued"} />}
      {(run.status === "failed" || run.status === "interrupted") && (
        <div className="progress">
          <h2>This check didn't finish</h2>
          <p className="err">{run.error}</p>
          <p className="small">Fix the problem above, then upload the manuscript again from the list of checks.</p>
        </div>
      )}
      {(run.status === "awaiting_human_signoff" || isSignedOff(run.status)) && (
        <Results run={run} events={events} onSigned={(r) => { setRun(r); api.audit(id).then((a) => setEvents(a.events)); }} />
      )}
    </main>
  );
}

/* ---------- progress while the graph runs ---------- */

function Progress({ events, queued }: { events: AuditEvent[]; queued: boolean }) {
  const seen = new Set(events.map((e) => e.event));
  const lookups = events.filter((e) => e.event === "http_get").length;
  const refs = events.filter((e) => e.event === "existence_verdict").length;
  const pairs = events.filter((e) => e.event === "support_verdict").length;
  const stages = [
    { label: "Read the manuscript", done: seen.has("parsed") },
    { label: `Look up references${refs ? ` (${refs} done, ${lookups} database requests)` : ""}`, done: seen.has("support_verdict") || seen.has("report_prepared") },
    { label: `Test each cited claim against its source${pairs ? ` (${pairs} done)` : ""}`, done: seen.has("report_prepared") },
    { label: "Prepare the results for review", done: seen.has("report_prepared") },
  ];
  const now = stages.findIndex((st) => !st.done);
  return (
    <section className="progress" aria-live="polite">
      <h2>{queued ? "Waiting for the previous check to finish" : "Checking"}</h2>
      <p className="small">
        Live checks take a few minutes: arXiv asks for three seconds between requests, and the local model reads each
        source in turn. You can leave this page.
      </p>
      <ol className="steps">
        {stages.map((st, i) => (
          <li key={st.label} className={st.done ? "done" : !queued && i === now ? "now" : ""}>{st.label}</li>
        ))}
      </ol>
    </section>
  );
}

/* ---------- results: proof sheet + sign-off ---------- */

function Results({ run, events, onSigned }: { run: RunDetail; events: AuditEvent[]; onSigned: (r: RunDetail) => void }) {
  const reviewing = run.status === "awaiting_human_signoff";
  const [tab, setTab] = useState<"proof" | "audit">("proof");
  const [onlyAttention, setOnlyAttention] = useState(reviewing);
  const [open, setOpen] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>({});

  const supportByClaim = useMemo(() => {
    const m: Record<string, Support[]> = {};
    for (const s of run.support) (m[s.claim_id] ??= []).push(s);
    return m;
  }, [run.support]);
  const titles = useMemo(
    () => Object.fromEntries(run.existence.map((e) => [e.reference.key, e.record?.title ?? e.reference.title ?? null])),
    [run.existence],
  );
  const human = useMemo(() => {
    const m: Record<string, HumanResolution> = {};
    for (const r of run.resolutions) m[r.item] = r;
    return m;
  }, [run.resolutions]);

  const flagged = [
    ...(run.pending?.references_needing_attention.map((k) => `R${k}`) ??
      run.existence.filter((e) => e.verdict !== "verified").map((e) => `R${e.reference.key}`)),
    ...(run.pending?.claim_pairs_needing_attention ??
      run.support.filter((s) => s.verdict !== "supported").map((s) => `${s.claim_id}/${s.reference_key}`)),
  ];
  const unresolved = new Set(run.unresolved_citations);
  const claimNeedsAttention = (c: Claim) =>
    c.cited_keys.some((k) => unresolved.has(k)) ||
    (supportByClaim[c.id] ?? []).some((s) => s.verdict !== "supported");

  const refs = onlyAttention ? run.existence.filter((e) => e.verdict !== "verified") : run.existence;
  const claims = onlyAttention ? run.claims.filter(claimNeedsAttention) : run.claims;
  const c = run.counts;

  const setItem = (item: string, verdict: string, note: string) =>
    setDraft((d) => {
      const next = { ...d };
      if (!verdict) delete next[item];
      else next[item] = { verdict, note };
      return next;
    });

  return (
    <>
      <div className="tally">
        <div><b>{c.verified} of {c.references}</b><span>references verified</span></div>
        <div className={c.reference_problems ? "red" : ""}><b>{c.reference_problems}</b><span>references with a problem</span></div>
        <div><b>{c.supported} of {c.pairs}</b><span>claim–source pairs supported</span></div>
        <div className={c.pairs_needing_attention ? "blue" : ""}><b>{c.pairs_needing_attention}</b><span>pairs needing attention</span></div>
      </div>

      <div className="workspace">
        <div>
          <div className="tabs" role="tablist">
            <button role="tab" aria-selected={tab === "proof"} onClick={() => setTab("proof")}>Manuscript</button>
            <button role="tab" aria-selected={tab === "audit"} onClick={() => setTab("audit")}>Audit trail</button>
            {tab === "proof" && (
              <label className="filter">
                <input type="checkbox" checked={onlyAttention} onChange={(e) => setOnlyAttention(e.target.checked)} />
                Only items that need attention
              </label>
            )}
          </div>

          {tab === "proof" ? (
            <div className="sheet">
              {run.unresolved_citations.length > 0 && (
                <div className="integrity">
                  Cited in the text but missing from the reference list: [{run.unresolved_citations.join("], [")}]
                </div>
              )}
              {run.uncited_references.length > 0 && (
                <div className="integrity">
                  In the reference list but never cited: [{run.uncited_references.join("], [")}]
                </div>
              )}

              <section className="sheet-section">
                <h2>Cited claims</h2>
                <p className="small">
                  Each sentence is tested against the abstract of every source it cites. Select a mark in the margin to
                  see the evidence{reviewing ? " and give your own verdict" : ""}.
                </p>
                {claims.length === 0 && <p className="small">Nothing here needs attention.</p>}
                {claims.map((cl) => (
                  <ClaimEntry
                    key={cl.id}
                    claim={cl}
                    support={supportByClaim[cl.id] ?? []}
                    missing={cl.cited_keys.filter((k) => unresolved.has(k))}
                    titles={titles}
                    human={human}
                    draft={draft}
                    open={open}
                    setOpen={setOpen}
                    reviewing={reviewing}
                    setItem={setItem}
                  />
                ))}
              </section>

              <section className="sheet-section">
                <h2>References</h2>
                <p className="small">Looked up in Crossref, OpenAlex, arXiv and PubMed.</p>
                {refs.length === 0 && <p className="small">Every reference was verified.</p>}
                {refs.map((e) => (
                  <ReferenceEntry
                    key={e.reference.key}
                    e={e}
                    human={human[`R${e.reference.key}`]}
                    draft={draft[`R${e.reference.key}`]}
                    open={open === `R${e.reference.key}`}
                    toggle={() => setOpen(open === `R${e.reference.key}` ? null : `R${e.reference.key}`)}
                    reviewing={reviewing}
                    setItem={setItem}
                  />
                ))}
              </section>
            </div>
          ) : (
            <Audit runId={run.run_id} events={events} />
          )}
        </div>

        <SignOff run={run} flagged={flagged} draft={draft} onSigned={onSigned} />
      </div>
    </>
  );
}

function CitedText({ text }: { text: string }) {
  const parts = text.split(/(\[\d+(?:\s*[-–,]\s*\d+)*\])/g);
  return (
    <>
      {parts.map((p, i) => (/^\[\d/.test(p) ? <span key={i} className="cite">{p}</span> : <Fragment key={i}>{p}</Fragment>))}
    </>
  );
}

function Mark(props: {
  verdict: string;
  sub?: string;
  human?: HumanResolution;
  draft?: { verdict: string };
  expanded: boolean;
  onClick: () => void;
}) {
  const v = verdictInfo(props.verdict);
  const you = props.draft ?? props.human;
  return (
    <button className={`mark ${v.tone}`} aria-expanded={props.expanded} onClick={props.onClick} title={v.help}>
      <span className="lbl">{v.label}</span>
      {props.sub && <span className="sub">{props.sub}</span>}
      {you && (
        <span className="you">
          {props.draft ? "Your verdict" : `${props.human!.reviewer}`}: <em>{verdictInfo(you.verdict).label.toLowerCase()}</em>
        </span>
      )}
    </button>
  );
}

function Resolve(props: {
  item: string;
  options: string[];
  draft?: { verdict: string; note: string };
  setItem: (item: string, verdict: string, note: string) => void;
}) {
  const verdict = props.draft?.verdict ?? "";
  const note = props.draft?.note ?? "";
  return (
    <div className="resolve">
      <label htmlFor={`v-${props.item}`}>Your verdict</label>
      <select id={`v-${props.item}`} value={verdict} onChange={(e) => props.setItem(props.item, e.target.value, note)}>
        <option value="">Leave the machine's verdict</option>
        {props.options.map((o) => (
          <option key={o} value={o}>{verdictInfo(o).label}</option>
        ))}
      </select>
      <label htmlFor={`n-${props.item}`}>Reason</label>
      <input
        id={`n-${props.item}`}
        value={note}
        disabled={!verdict}
        placeholder={verdict ? "e.g. checked the full text, p. 4" : "Choose a verdict first"}
        onChange={(e) => props.setItem(props.item, verdict, e.target.value)}
      />
    </div>
  );
}

function HumanNote({ h }: { h?: HumanResolution }) {
  if (!h) return null;
  return (
    <p className="why">
      <strong>{h.reviewer}</strong> marked this {verdictInfo(h.verdict).label.toLowerCase()}
      {h.note ? `: ${h.note}` : "."}
    </p>
  );
}

function ClaimEntry(props: {
  claim: Claim;
  support: Support[];
  missing: string[];
  titles: Record<string, string | null>;
  human: Record<string, HumanResolution>;
  draft: Draft;
  open: string | null;
  setOpen: (k: string | null) => void;
  reviewing: boolean;
  setItem: (item: string, verdict: string, note: string) => void;
}) {
  const { claim, support } = props;
  const openPair = support.find((s) => props.open === `${s.claim_id}/${s.reference_key}`);
  return (
    <div className="entry">
      <div className="key">{claim.id.slice(1)}</div>
      <div className="text"><CitedText text={claim.text} /></div>
      <div className="margin">
        {support.map((s) => {
          const item = `${s.claim_id}/${s.reference_key}`;
          return (
            <Mark
              key={item}
              verdict={s.verdict}
              sub={`against source [${s.reference_key}]`}
              human={props.human[item]}
              draft={props.draft[item]}
              expanded={props.open === item}
              onClick={() => props.setOpen(props.open === item ? null : item)}
            />
          );
        })}
        {props.missing.map((k) => (
          <div key={k} className="mark red" style={{ cursor: "default" }}>
            <span className="lbl">No reference [{k}]</span>
            <span className="sub">not in the reference list</span>
          </div>
        ))}
      </div>
      {openPair && (
        <div className="detail">
          <h4>Source [{openPair.reference_key}]: {props.titles[openPair.reference_key] ?? "untitled"}</h4>
          {openPair.evidence_quote && <blockquote>“{openPair.evidence_quote}”</blockquote>}
          {openPair.rationale && <p className="why" style={{ margin: 0 }}>{openPair.rationale}</p>}
          <span className="small">
            Checked by {openPair.method.replace("ollama:", "")} against the {openPair.evidence_scope} only.
          </span>
          <HumanNote h={props.human[`${openPair.claim_id}/${openPair.reference_key}`]} />
          {props.reviewing && (
            <Resolve
              item={`${openPair.claim_id}/${openPair.reference_key}`}
              options={SUPPORT_VERDICTS}
              draft={props.draft[`${openPair.claim_id}/${openPair.reference_key}`]}
              setItem={props.setItem}
            />
          )}
        </div>
      )}
    </div>
  );
}

function ReferenceEntry(props: {
  e: Existence;
  human?: HumanResolution;
  draft?: { verdict: string; note: string };
  open: boolean;
  toggle: () => void;
  reviewing: boolean;
  setItem: (item: string, verdict: string, note: string) => void;
}) {
  const { e } = props;
  const sim = e.title_similarity != null ? `title match ${Math.round(e.title_similarity * 100)}%` : undefined;
  return (
    <div className="entry">
      <div className="key">[{e.reference.key}]</div>
      <div className="text">{e.reference.raw}</div>
      <div className="margin">
        <Mark verdict={e.verdict} sub={sim} human={props.human} draft={props.draft} expanded={props.open} onClick={props.toggle} />
      </div>
      {props.open && (
        <div className="detail">
          {e.record ? (
            <h4>
              Found in {e.record.source}:{" "}
              {e.record.url ? <a href={e.record.url} target="_blank" rel="noreferrer">{e.record.title}</a> : e.record.title}
              {e.record.year ? ` (${e.record.year})` : ""}
            </h4>
          ) : (
            <h4>No matching record</h4>
          )}
          {e.issues.length > 0 && <ul>{e.issues.map((i) => <li key={i}>{i}</li>)}</ul>}
          <HumanNote h={props.human} />
          {props.reviewing && (
            <Resolve item={`R${e.reference.key}`} options={REFERENCE_VERDICTS} draft={props.draft} setItem={props.setItem} />
          )}
        </div>
      )}
    </div>
  );
}

/* ---------- sign-off ---------- */

function readReviewer() {
  try {
    return localStorage.getItem("rra.reviewer") ?? "";
  } catch {
    return "";
  }
}

function SignOff({ run, flagged, draft, onSigned }: { run: RunDetail; flagged: string[]; draft: Draft; onSigned: (r: RunDetail) => void }) {
  const [reviewer, setReviewer] = useState(readReviewer);
  const [decision, setDecision] = useState<Decision | "">("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const last = run.signoffs[run.signoffs.length - 1];
  if (last) {
    const tone = last.decision === "reject" ? "red" : last.decision === "approve" ? "ok" : "ochre";
    return (
      <aside className="signoff">
        <div className={`stamp ${tone}`}>
          <strong>{DECISION_LABEL[last.decision]}</strong>
          <p>by {last.reviewer}, {when(last.ts)}</p>
          {last.note && <p>{last.note}</p>}
        </div>
        <p className="small">
          {run.resolutions.length} item{run.resolutions.length === 1 ? "" : "s"} given a reviewer's verdict. The decision is
          recorded in the audit trail and can't be changed.
        </p>
      </aside>
    );
  }

  const decided = flagged.filter((f) => draft[f]).length;
  const sign = async () => {
    if (!decision) return;
    setBusy(true);
    setError(null);
    try {
      localStorage.setItem("rra.reviewer", reviewer);
    } catch {
      /* storage unavailable; fine */
    }
    try {
      const r = await api.review(run.run_id, {
        reviewer: reviewer.trim(),
        decision,
        note,
        resolutions: Object.entries(draft).map(([item, d]) => ({ item, verdict: d.verdict, note: d.note })),
      });
      onSigned(r);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <aside className="signoff" aria-label="Sign off">
      <h2>Sign off this check</h2>
      <div className="queue">
        <b>{flagged.length}</b> flagged item{flagged.length === 1 ? "" : "s"}. You've given your verdict on {decided}.
        {Object.keys(draft).length > decided && ` Plus ${Object.keys(draft).length - decided} you overrode yourself.`}
      </div>
      <div className="field">
        <label className="t" htmlFor="reviewer">Your name</label>
        <input id="reviewer" type="text" value={reviewer} onChange={(e) => setReviewer(e.target.value)} />
      </div>
      <div className="field">
        <span className="t">Decision</span>
        <div className="decision" role="radiogroup">
          {(["approve", "approve_with_changes", "reject"] as Decision[]).map((d) => (
            <label key={d}>
              <input type="radio" name="decision" checked={decision === d} onChange={() => setDecision(d)} />
              {d === "approve" ? "Approve" : d === "approve_with_changes" ? "Approve with changes" : "Reject"}
            </label>
          ))}
        </div>
      </div>
      <div className="field">
        <label className="t" htmlFor="note">Note for the authors</label>
        <textarea id="note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="What needs to change, if anything" />
      </div>
      {decision === "approve" && decided < flagged.length && (
        <p className="small">
          {flagged.length - decided} flagged item{flagged.length - decided === 1 ? " has" : "s have"} no verdict from you.
          They'll be recorded with the machine's verdict.
        </p>
      )}
      {error && <p className="err">{error}</p>}
      <button className="btn" disabled={!reviewer.trim() || !decision || busy} onClick={sign}>
        {busy ? "Signing off…" : "Sign off"}
      </button>
      <p className="small">Signing off is final. It's recorded in the audit trail with your name.</p>
    </aside>
  );
}

/* ---------- audit trail ---------- */

function summarise(e: AuditEvent): string {
  const d = e.data as Record<string, any>;
  switch (e.event) {
    case "source_converted":
      return `${d.filename} converted with ${d.extractor}${d.edited_by_reviewer ? ", edited before checking" : ""}. Original ${String(d.original_sha256).slice(0, 12)}…`;
    case "run_started":
      return `Mode ${d.mode}${d.model?.model ? `, model ${d.model.model}` : ""}${d.model?.digest ? ` (${String(d.model.digest).slice(0, 19)}…)` : ""}`;
    case "parsed":
      return `${d.references?.length ?? 0} references, ${d.claims?.length ?? 0} cited claims`;
    case "http_get":
      return `${d.status ?? "—"} ${d.cache === "hit" ? "(cached) " : ""}${d.url}`;
    case "http_error":
      return `${d.error}: ${d.url}`;
    case "llm_call":
      return `${d.model}${d.cache === "hit" ? " (cached)" : ""}, prompt ${String(d.prompt_sha256).slice(0, 12)}…`;
    case "existence_verdict":
      return `Reference [${d.key}]: ${verdictInfo(d.verdict).label}`;
    case "support_verdict":
      return `${d.claim_id} against [${d.reference_key}]: ${verdictInfo(d.verdict).label}`;
    case "human_resolution":
      return `${d.reviewer} marked ${d.item} ${verdictInfo(d.verdict).label.toLowerCase()}${d.note ? `: ${d.note}` : ""}`;
    case "human_signoff":
      return `${d.reviewer}: ${DECISION_LABEL[d.decision as Decision] ?? d.decision}${d.note ? `. ${d.note}` : ""}`;
    default:
      return d.status ? String(d.status) : d.message ? String(d.message) : "";
  }
}

function Audit({ runId, events }: { runId: string; events: AuditEvent[] }) {
  const [state, setState] = useState<{ ok: boolean | null; message: string } | null>(null);
  useEffect(() => {
    api.audit(runId).then((a) => setState({ ok: a.ok, message: a.message }), () => null);
  }, [runId, events.length]);
  return (
    <div className="sheet">
      <section className="sheet-section">
        <h2>Audit trail</h2>
        <p className="small">
          Every lookup, model call and decision, in order. Each entry carries the hash of the one before it, so any later
          edit breaks the chain.
        </p>
      </section>
      <div className="chain">
        {state && (
          <span className={`status ${state.ok ? "ok" : "red"}`}>{state.ok ? "Chain intact" : "Chain broken"}</span>
        )}
        <span className="small">{state?.message}</span>
      </div>
      <table className="events">
        <tbody>
          {events.map((e) => (
            <tr key={e.seq}>
              <td className="num">{e.seq}</td>
              <td className="ev">{e.event.replace(/_/g, " ")}</td>
              <td className="sum">{summarise(e)}</td>
              <td className="h">{e.hash.slice(0, 10)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
