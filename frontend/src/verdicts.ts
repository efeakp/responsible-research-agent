// Plain-language labels and the "pencil" each verdict is marked in.
// green = fine, red = a real problem, blue = a person has to look, ochre = caution, grey = can't tell.

export type Tone = "ok" | "red" | "blue" | "ochre" | "grey";

const V: Record<string, { label: string; tone: Tone; help: string }> = {
  verified: { label: "Verified", tone: "ok", help: "Identifier resolves; title, first author and year match." },
  metadata_mismatch: {
    label: "Doesn't match",
    tone: "red",
    help: "The identifier points to a work whose title, author or year differ from the reference.",
  },
  found_by_search: {
    label: "Found by title",
    tone: "ochre",
    help: "No identifier given. A confident title match was found; add its identifier.",
  },
  not_found: { label: "Not found", tone: "red", help: "Nothing in Crossref, OpenAlex, arXiv or PubMed matches." },
  retracted: { label: "Retracted", tone: "red", help: "This work has been retracted." },
  unchecked: { label: "Couldn't check", tone: "grey", help: "A lookup failed. This is not the same as missing." },
  supported: { label: "Supported", tone: "ok", help: "The model's quote appears word for word in the source." },
  partially_supported: {
    label: "Partly supported",
    tone: "ochre",
    help: "The source supports part of it; the claim overstates, generalises or changes numbers.",
  },
  not_supported: { label: "Not supported", tone: "red", help: "The source doesn't say this." },
  insufficient_evidence: { label: "No text to check", tone: "grey", help: "No abstract was available." },
  needs_human_review: {
    label: "Needs a person",
    tone: "blue",
    help: "The model's evidence failed the quote check, or no model was used.",
  },
};

export const verdictInfo = (v: string) =>
  V[v] ?? { label: v.replace(/_/g, " "), tone: "grey" as Tone, help: "" };

export const REFERENCE_VERDICTS = [
  "verified",
  "metadata_mismatch",
  "found_by_search",
  "not_found",
  "retracted",
  "unchecked",
];
export const SUPPORT_VERDICTS = [
  "supported",
  "partially_supported",
  "not_supported",
  "insufficient_evidence",
  "needs_human_review",
];

export const DECISION_LABEL = {
  approve: "Approved",
  approve_with_changes: "Approved with changes",
  reject: "Rejected",
} as const;
