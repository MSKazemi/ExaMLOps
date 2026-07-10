// Self-hosted glossary for the contextual help drawer (F21 / ADR 0072, R6). Plain data — no SaaS.

export interface GlossaryTerm {
  term: string
  definition: string
}

export const GLOSSARY: GlossaryTerm[] = [
  { term: 'Drift', definition: 'A statistically significant shift in a model’s prediction distribution vs. its baseline.' },
  { term: 'Promotion', definition: 'Moving a model version to a serving alias (Staging → Production) after passing metric gates.' },
  { term: 'Alias', definition: 'A named pointer (Production / Canary / Staging) to a specific model version, used for routing.' },
  { term: 'GPU-hours', definition: 'Cumulative GPU wall-clock time consumed by training/serving — the basis for cost and carbon.' },
  { term: 'Canary', definition: 'A small traffic slice routed to a new version to validate it before full rollout.' },
  { term: 'Approval gate', definition: 'A human sign-off step required before a sensitive action (e.g. retrain) executes.' },
  { term: 'Baseline', definition: 'A stored reference distribution/metric that later observations are compared against.' },
  { term: 'BFF', definition: 'Backend-for-Frontend — an aggregation layer that composes several backends into one dashboard response.' },
]

/** Case-insensitive substring search over terms + definitions (R6). */
export function searchGlossary(query: string): GlossaryTerm[] {
  const q = query.trim().toLowerCase()
  if (!q) return GLOSSARY
  return GLOSSARY.filter((t) => `${t.term} ${t.definition}`.toLowerCase().includes(q))
}
