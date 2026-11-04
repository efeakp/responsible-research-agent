import { useCallback, useEffect, useRef, useState } from "react";
import { api, isActive, type ConversionOut, type Health, type RunSummary, type Summary } from "./api";
import { statusInfo, when } from "./format";

export default function RunsPage() {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api.runs().then(setRuns, (e) => setError(e.message));
  }, []);

  useEffect(() => {
    load();
    api.health().then(setHealth, () => setHealth(null));
  }, [load]);

  // keep the list fresh while anything is still running
  useEffect(() => {
    if (!runs?.some((r) => isActive(r.status))) return;
    const t = setInterval(load, 2500);
    return () => clearInterval(t);
  }, [runs, load]);

  return (
    <main className="page">
      <h1>Check a manuscript's citations</h1>
      <p className="lede">
        Every reference is looked up in Crossref, OpenAlex, arXiv and PubMed, and every cited sentence is tested against
        the source it cites. Nothing is final until a person signs it off.
      </p>

      <Intake health={health} />

      <h2>Checks</h2>
      {error && <p className="err">{error}</p>}
      {runs && runs.length === 0 && (
        <div className="empty">No checks yet. Drop a manuscript above to start one.</div>
      )}
      {runs && runs.length > 0 && (
        <table className="runs">
          <thead>
            <tr>
              <th>Manuscript</th>
              <th className="hide-sm">Started</th>
              <th>Status</th>
              <th className="hide-sm">References verified</th>
              <th className="hide-sm">Claims needing attention</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => {
              const s = statusInfo(r.status);
              const done = r.counts.references > 0;
              return (
                <tr key={r.run_id} onClick={() => (window.location.hash = `#/runs/${r.run_id}`)}>
                  <td>
                    <a className="fname" href={`#/runs/${r.run_id}`} onClick={(e) => e.stopPropagation()}>
                      {r.filename}
                    </a>
                    <div className="small">{r.mode === "ollama" ? r.model : "No model"}</div>
                  </td>
                  <td className="hide-sm">{when(r.created_at)}</td>
                  <td>
                    <span className={`status ${s.tone} ${s.live ? "live" : ""}`}>{s.label}</span>
                  </td>
                  <td className="hide-sm num">{done ? `${r.counts.verified} of ${r.counts.references}` : "—"}</td>
                  <td className="hide-sm num">{done ? r.counts.pairs_needing_attention : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </main>
  );
}

const TEXT_TYPES = /\.(md|markdown|txt)$/i;
const CONVERT_TYPES = /\.(pdf|docx|html?|xhtml)$/i;

function Intake({ health }: { health: Health | null }) {
  const [file, setFile] = useState<File | null>(null);
  const [over, setOver] = useState(false);
  const [mode, setMode] = useState<"ollama" | "heuristic">("ollama");
  const [model, setModel] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conversion, setConversion] = useState<ConversionOut | null>(null);
  const input = useRef<HTMLInputElement>(null);

  const models = health?.ollama.models ?? [];
  const ollamaOk = !!health?.ollama.reachable && models.length > 0;

  useEffect(() => {
    if (!health) return;
    if (!model) setModel(models.includes(health.default_model) ? health.default_model : models[0] ?? "");
    if (!ollamaOk) setMode("heuristic");
  }, [health]); // eslint-disable-line react-hooks/exhaustive-deps

  const pick = (f: File | undefined) => {
    setError(null);
    if (!f) return;
    if (!TEXT_TYPES.test(f.name) && !CONVERT_TYPES.test(f.name)) {
      setError("Use a PDF, Word (.docx), HTML, Markdown or plain-text file.");
      return;
    }
    setFile(f);
  };

  const needsConversion = !!file && CONVERT_TYPES.test(file.name);

  const go = async () => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      if (needsConversion) {
        setConversion(await api.convert(file));
        setBusy(false);
        return;
      }
      const { run_id } = await api.createRun(file, mode, mode === "ollama" ? model : undefined);
      window.location.hash = `#/runs/${run_id}`;
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  const options = (
    <Options health={health} mode={mode} setMode={setMode} model={model} setModel={setModel} ollamaOk={ollamaOk} />
  );

  if (conversion) {
    return (
      <section className="intake with-preview" aria-label="Check the converted text">
        <Preview
          conversion={conversion}
          mode={mode}
          model={model}
          onCancel={() => {
            setConversion(null);
            setFile(null);
          }}
        />
        <div className="options">{options}</div>
      </section>
    );
  }

  return (
    <section className="intake" aria-label="Start a check">
      <div>
        <button
          type="button"
          className={`drop ${over ? "over" : ""}`}
          onClick={() => input.current?.click()}
          onDragOver={(e) => {
            e.preventDefault();
            setOver(true);
          }}
          onDragLeave={() => setOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setOver(false);
            pick(e.dataTransfer.files[0]);
          }}
        >
          {file ? (
            <>
              <span className="file">{file.name}</span>
              <span className="small"> — {(file.size / 1024).toFixed(1)} KB. Click to choose a different file.</span>
              {needsConversion && (
                <span className="small" style={{ display: "block", marginTop: 6 }}>
                  This will be converted to text first. You'll see the result, and can correct it, before the check
                  starts.
                </span>
              )}
            </>
          ) : (
            <>
              <strong>Drop an article here, or click to choose one</strong>
              <span className="small">
                PDF, Word or HTML, converted to text for you, or Markdown with numbered citations like [1] and a
                References section.
              </span>
            </>
          )}
        </button>
        <input
          ref={input}
          type="file"
          accept=".pdf,.docx,.html,.htm,.md,.markdown,.txt"
          hidden
          onChange={(e) => pick(e.target.files?.[0])}
        />
        {error && <p className="err">{error}</p>}
      </div>

      <div className="options">
        {options}
        <div>
          <button className="btn" disabled={!file || busy || !health} onClick={go}>
            {busy ? (needsConversion ? "Converting…" : "Starting…") : needsConversion ? "Convert and preview" : "Start check"}
          </button>
        </div>
      </div>
    </section>
  );
}

function Options(props: {
  health: Health | null;
  mode: "ollama" | "heuristic";
  setMode: (m: "ollama" | "heuristic") => void;
  model: string;
  setModel: (m: string) => void;
  ollamaOk: boolean;
}) {
  const { health, mode, setMode, model, setModel, ollamaOk } = props;
  const models = health?.ollama.models ?? [];
  return (
    <>
      <fieldset>
        <legend>How should claims be checked?</legend>
        <label className="choice">
          <input type="radio" name="mode" checked={mode === "ollama"} disabled={!ollamaOk} onChange={() => setMode("ollama")} />
          <span>
            With a local model
            <small>Runs on this machine through Ollama. Claims are only marked supported with a word-for-word quote.</small>
            {ollamaOk && (
              <select value={model} onChange={(e) => setModel(e.target.value)} aria-label="Model">
                {models.map((m) => (
                  <option key={m}>{m}</option>
                ))}
              </select>
            )}
          </span>
        </label>
        <label className="choice">
          <input type="radio" name="mode" checked={mode === "heuristic"} onChange={() => setMode("heuristic")} />
          <span>
            Without a model
            <small>References are still fully checked. Every cited claim goes to a person.</small>
          </span>
        </label>
      </fieldset>
      {health === null ? (
        <div className="engine-note bad">Can't reach the API server. Start it with <code>rra serve</code>.</div>
      ) : !health.ollama.reachable ? (
        <div className="engine-note bad">
          Ollama isn't running at {health.ollama.host}. Start it with <code>ollama serve</code> to check claims with a
          model.
        </div>
      ) : models.length === 0 ? (
        <div className="engine-note bad">
          Ollama is running but has no models. Pull one with <code>ollama pull {health.default_model}</code>.
        </div>
      ) : null}
    </>
  );
}

function Preview(props: { conversion: ConversionOut; mode: "ollama" | "heuristic"; model: string; onCancel: () => void }) {
  const { conversion } = props;
  const [text, setText] = useState(conversion.markdown);
  const [summary, setSummary] = useState<Summary>(conversion.summary);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const edited = text !== conversion.markdown;

  // refresh the counts shortly after the reviewer stops typing
  useEffect(() => {
    if (!edited) {
      setSummary(conversion.summary);
      return;
    }
    const ctl = new AbortController();
    const t = setTimeout(() => {
      api.inspect(text, ctl.signal).then(setSummary, () => undefined);
    }, 500);
    return () => {
      clearTimeout(t);
      ctl.abort();
    };
  }, [text, edited, conversion.summary]);

  const blocking = summary.references === 0 || summary.claims === 0;

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const { run_id } = await api.createRunFromConversion(
        conversion.conversion_id,
        text,
        props.mode,
        props.mode === "ollama" ? props.model : undefined,
      );
      window.location.hash = `#/runs/${run_id}`;
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <div className="preview">
      <div className="preview-head">
        <div>
          <h2>Check the converted text</h2>
          <p className="small">
            {conversion.filename}, converted with {conversion.extractor}. This is exactly what will be checked. Correct
            anything the conversion got wrong before you start.
          </p>
        </div>
      </div>

      <div className="tally compact" aria-live="polite">
        <div className={summary.references === 0 ? "red" : ""}>
          <b>{summary.references}</b>
          <span>references found</span>
        </div>
        <div>
          <b>{summary.with_identifier}</b>
          <span>with a DOI, arXiv ID or PMID</span>
        </div>
        <div className={summary.claims === 0 ? "red" : ""}>
          <b>{summary.claims}</b>
          <span>sentences with citations</span>
        </div>
        <div className={summary.missing_citations.length ? "red" : ""}>
          <b>{summary.missing_citations.length}</b>
          <span>citations with no reference</span>
        </div>
      </div>

      {(conversion.warnings.length > 0 || summary.missing_citations.length > 0 || blocking) && (
        <ul className="conv-warnings">
          {summary.references === 0 && (
            <li>
              No reference list found. Add a line reading <code>## References</code> above it, with one numbered entry
              per line.
            </li>
          )}
          {summary.claims === 0 && summary.references > 0 && (
            <li>No citations like [1] found in the text, so there are no claims to check against sources.</li>
          )}
          {summary.missing_citations.length > 0 && (
            <li>
              Cited in the text but missing from the reference list: [{summary.missing_citations.slice(0, 12).join("], [")}]
              {summary.missing_citations.length > 12 ? " and more" : ""}.
            </li>
          )}
          {conversion.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}

      <label className="small" htmlFor="md">
        Converted text{edited ? " (edited)" : ""}
      </label>
      <textarea id="md" className="md-edit" value={text} spellCheck={false} onChange={(e) => setText(e.target.value)} />

      {error && <p className="err">{error}</p>}
      <div className="preview-actions">
        <button className="btn" disabled={busy || blocking} onClick={start}>
          {busy ? "Starting…" : "Start check"}
        </button>
        {edited && (
          <button className="btn ghost" onClick={() => setText(conversion.markdown)} disabled={busy}>
            Undo my edits
          </button>
        )}
        <button className="link-btn" onClick={props.onCancel} disabled={busy}>
          Choose a different file
        </button>
      </div>
      {blocking && <p className="small">The check can start once the text has both a reference list and citations.</p>}
      {edited && !blocking && (
        <p className="small">Your edits are recorded in the audit trail, alongside a fingerprint of the original file.</p>
      )}
    </div>
  );
}
