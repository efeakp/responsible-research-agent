import type { RunStatus } from "./api";
import { DECISION_LABEL, type Tone } from "./verdicts";

export function when(iso: string) {
  const d = new Date(iso);
  return d.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function statusInfo(s: RunStatus): { label: string; tone: Tone; live?: boolean } {
  if (s === "queued") return { label: "Queued", tone: "grey", live: true };
  if (s === "running") return { label: "Checking", tone: "blue", live: true };
  if (s === "failed") return { label: "Failed", tone: "red" };
  if (s === "interrupted") return { label: "Stopped", tone: "grey" };
  if (s === "awaiting_human_signoff") return { label: "Ready for review", tone: "blue" };
  const d = s.split(":")[1] as keyof typeof DECISION_LABEL;
  return { label: DECISION_LABEL[d] ?? "Signed off", tone: d === "reject" ? "red" : d === "approve" ? "ok" : "ochre" };
}
