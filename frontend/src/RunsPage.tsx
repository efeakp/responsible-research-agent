import { useCallback, useEffect, useRef, useState } from "react";
import { api, isActive, type Health, type RunSummary } from "./api";
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

function Intake({ health }: { health: Health | null }) {
  const [file, setFile] = useState<File | null>(null);
  const [over, setOver] = useState(false);
  const [mode, setMode] = useState<"ollama" | "heuristic">("ollama");
  const [model, setModel] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
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
    if (!/\.(md|markdown|txt)$/i.test(f.name)) {
      setError("Use a Markdown or plain-text file (.md or .txt).");
      return;
    }
    setFile(f);
  };

  const start = async () => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const { run_id } = await api.createRun(file, mode, mode === "ollama" ? model : undefined);
      window.location.hash = `#/runs/${run_id}`;
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

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
            </>
          ) : (
            <>
              <strong>Drop a manuscript here, or click to choose one</strong>
              <span className="small">
                Markdown or plain text, with numbered citations like [1] and a References section at the end.
              </span>
            </>
          )}
        </button>
        <input
          ref={input}
          type="file"
          accept=".md,.markdown,.txt,text/markdown,text/plain"
          hidden
          onChange={(e) => pick(e.target.files?.[0])}
        />
        {error && <p className="err">{error}</p>}
      </div>

      <div className="options">
        <fieldset>
          <legend>How should claims be checked?</legend>
          <label className="choice">
            <input
              type="radio"
              name="mode"
              checked={mode === "ollama"}
              disabled={!ollamaOk}
              onChange={() => setMode("ollama")}
            />
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
            Ollama isn't running at {health.ollama.host}. Start it with <code>ollama serve</code> to check claims with
            a model.
          </div>
        ) : models.length === 0 ? (
          <div className="engine-note bad">
            Ollama is running but has no models. Pull one with <code>ollama pull {health.default_model}</code>.
          </div>
        ) : null}
        <div>
          <button className="btn" disabled={!file || busy || !health} onClick={start}>
            {busy ? "Starting…" : "Start check"}
          </button>
        </div>
      </div>
    </section>
  );
}
