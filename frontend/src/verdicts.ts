// Plain-language labels and the "pencil" each verdict is marked in.
// green = fine, red = a real problem, blue = a person has to look, ochre = caution, grey = can't tell.

export type Tone = "ok" | "red" | "blue" | "ochre" | "grey";

const V: Record<string, { label: string; tone: Tone; help: string }> = {
  verified: { label: "Verified", tone: "ok", help: "Identifier resolves; title, first author and year match." },
  metadata_mismatch: {
    label: "Doesn't match",
    tone: "red",
    help: "The matching record's title, authors or year differ from the reference. Check for errors in the reference.",
  },
  found_by_search: {
    label: "Found by title",
    tone: "ochre",
    help: "No identifier given. A confident title match was found; add its identifier.",
  },
  not_found: {
    label: "Not found",
    tone: "red",
    help: "Its DOI, ID or link doesn't exist, or it's formatted like a journal paper that no database has.",
  },
  exists_unindexed: {
    label: "Exists, not indexed",
    tone: "ochre",
    help: "A real DOI or working link, but not a scholarly record. Check it's the cited document.",
  },
  unverifiable: {
    label: "Can't verify",
    tone: "blue",
    help: "No DOI or link, and not in the scholarly databases. Common for reports and legislation. Check by hand.",
  },
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
  "exists_unindexed",
  "unverifiable",
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
