// Typed client for the FastAPI backend.

const BASE = (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/$/, "") ?? "";

export type RunStatus =
  | "queued"
  | "running"
  | "failed"
  | "interrupted"
  | "awaiting_human_signoff"
  | `signed_off:${Decision}`;

export type Decision = "approve" | "approve_with_changes" | "reject";

export interface Counts {
  references: number;
  verified: number;
  reference_problems: number;
  claims: number;
  pairs: number;
  supported: number;
  pairs_needing_attention: number;
}

export interface Source {
  filename: string;
  extractor: string;
  warnings: string[];
  edited: boolean;
  original_file: string;
}

export interface Summary {
  references: number;
  with_identifier: number;
  claims: number;
  missing_citations: string[];
  uncited_references: string[];
  has_reference_heading: boolean;
  citation_style?: "numeric" | "author_year" | "none";
}

export interface ConversionOut {
  conversion_id: string;
  filename: string;
  extractor: string;
  warnings: string[];
  markdown: string;
  summary: Summary;
}

export interface RunSummary {
  run_id: string;
  filename: string;
  created_at: string;
  mode: "ollama" | "heuristic";
  model: string | null;
  status: RunStatus;
  error: string | null;
  counts: Counts;
  source?: Source;
}

export interface Reference {
  key: string;
  raw: string;
  doi?: string | null;
  arxiv_id?: string | null;
  pmid?: string | null;
  title?: string | null;
  first_author?: string | null;
  year?: number | null;
}

export interface SourceRecord {
  source: string;
  identifier: string;
  title?: string | null;
  authors: string[];
  year?: number | null;
  abstract?: string | null;
  is_retracted: boolean;
  url?: string | null;
  volume?: string | null;
  issue?: string | null;
  first_page?: string | null;
  last_page?: string | null;
}

export interface Existence {
  reference: Reference;
  verdict: string;
  record: SourceRecord | null;
  title_similarity: number | null;
  issues: string[];
}

export interface Claim {
  id: string;
  text: string;
  cited_keys: string[];
  unresolved?: string[];
  plain?: string | null;
}

export interface Support {
  claim_id: string;
  reference_key: string;
  verdict: string;
  method: string;
  evidence_quote: string | null;
  rationale: string;
  evidence_scope: string;
}

export interface HumanResolution {
  item: string;
  verdict: string;
  note: string;
  reviewer: string;
  ts: string;
}

export interface Signoff {
  reviewer: string;
  decision: Decision;
  note: string;
  ts: string;
}

export interface RunDetail extends RunSummary {
  existence: Existence[];
  claims: Claim[];
  support: Support[];
  unresolved_citations: string[];
  uncited_references: string[];
  citation_style?: "numeric" | "author_year" | "none";
  pending: { references_needing_attention: string[]; claim_pairs_needing_attention: string[] } | null;
  resolutions: HumanResolution[];
  signoffs: Signoff[];
  verdict_options: string[];
  decisions: Decision[];
}

export interface AuditEvent {
  seq: number;
  ts: string;
  event: string;
  data: Record<string, unknown>;
  prev_hash: string;
  hash: string;
}

export interface Health {
  version: string;
  default_model: string;
  ollama: { host: string; reachable: boolean; models: string[]; error?: string };
}

export interface ReviewIn {
  reviewer: string;
  decision: Decision;
  note: string;
  resolutions: { item: string; verdict: string; note: string }[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, init);
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    throw new Error("Can't reach the API server. Start it with `rra serve`.");
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail)) detail = body.detail.map((d: { msg: string }) => d.msg).join("; ");
    } catch {
      /* keep status text */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => request<Health>("/api/health"),
  runs: () => request<RunSummary[]>("/api/runs"),
  run: (id: string) => request<RunDetail>(`/api/runs/${encodeURIComponent(id)}`),
  audit: (id: string) =>
    request<{ ok: boolean | null; message: string; events: AuditEvent[] }>(
      `/api/runs/${encodeURIComponent(id)}/audit`,
    ),
  reportUrl: (id: string) => `${BASE}/api/runs/${encodeURIComponent(id)}/report`,
  createRun: (file: File, mode: "ollama" | "heuristic", model?: string) => {
    const form = new FormData();
    form.append("file", file);
    form.append("mode", mode);
    if (model) form.append("model", model);
    return request<{ run_id: string }>("/api/runs", { method: "POST", body: form });
  },
  convert: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<ConversionOut>("/api/conversions", { method: "POST", body: form });
  },
  inspect: (markdown: string, signal?: AbortSignal) =>
    request<Summary>("/api/inspect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ markdown }),
      signal,
    }),
  createRunFromConversion: (conversionId: string, markdown: string, mode: "ollama" | "heuristic", model?: string) => {
    const form = new FormData();
    form.append("conversion_id", conversionId);
    form.append("markdown", markdown);
    form.append("mode", mode);
    if (model) form.append("model", model);
    return request<{ run_id: string }>("/api/runs", { method: "POST", body: form });
  },
  sourceUrl: (id: string) => `${BASE}/api/runs/${encodeURIComponent(id)}/source`,
  manuscriptUrl: (id: string) => `${BASE}/api/runs/${encodeURIComponent(id)}/manuscript`,
  review: (id: string, body: ReviewIn) =>
    request<RunDetail>(`/api/runs/${encodeURIComponent(id)}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
};

export const isActive = (s: RunStatus) => s === "queued" || s === "running";
export const isSignedOff = (s: RunStatus) => s.startsWith("signed_off");
