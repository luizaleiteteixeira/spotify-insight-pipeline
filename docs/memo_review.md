# Human review of the decision memo

The brief requires that "a person inspects the final argument." This is the record of that review for the full run (`runs/full/memo/`).

| Version | Prompt | What happened |
|---|---|---|
| v2 | `prompts/memo_v2.md` | First full-run memo. Passed the automatic number/ID checks, but the author's read found: a customer quote cut with "[...]"; no explanation that the top issue is a free-tier packaging decision; no disclosure of label bias toward the top issue; the #2 generic issue not discussed. |
| v3 | `prompts/memo_v3.md` | Prompt revised for those four points; code added an exact-quote check and passed the golden-set and verifier reliability numbers into the evidence pack. |
| v4 (final) | `prompts/memo_v4.md` | After reading v3, the author reviewed five proposed improvements (A-E) drafted with the AI coding assistant, **accepted A, B, C, D and rejected E**. |

## The author's decisions on v3 → v4

| | Proposed change (drafted with the AI assistant) | Author's decision |
|---|---|---|
| A | Open with the answer in one sentence a product leader could repeat | **Accepted** |
| B | Turn the recommendation into 2-3 concrete product options to test (not proven fixes) | **Accepted** |
| C | Use expressed cancellation intent (area and issue level) as the business-risk argument, never as churn | **Accepted** |
| D | Add the monthly trend of the top issue with denominators and partial-month caveats | **Accepted** |
| E | Expand the usability caveat (ads + redesign as one "free-tier experience" bundle) | **Rejected** |

For D, code added `top_issue_monthly` to the evidence pack: the top issue's monthly share of completed reviews, which stays between 3.28% and 4.76% from 2022-06 to 2023-05 and then rises to 17.04% (2023-09) and 29.22% (2023-10). See `runs/full/memo/evidence_pack.json`.

## Automatic checks on the final memo

`runs/full/memo/recommendation.json` → `check`: every claim ID exists in `claims.csv`; every issue and review ID is in the evidence pack; every number appears in the pack (no computed or rounded numbers); every quoted fragment is an exact excerpt of pack text; the memo is not truncated (≥ 350 words, ends in a full sentence).

While building v4, the checks rejected real defects: rounded shares ("3.3-4.8%"), and an output that stopped mid-sentence. They also produced false positives that were fixed in the checker, not the memo: a range dash read as a minus sign, a quote ending in a comma inside the quotation marks, and mismatched straight and curly quote pairing. Every attempt is logged in `recommendation.json` → `attempts` and in `runs/ledger.jsonl` (stage `memo`).

One formatting-only post-processing step: the model twice wrote the em dash as the literal six-character escape `\u2014`. The writer converts it to the character. The content is unchanged.

**Status of the pre-iteration v4: APPROVED by the author on 2026-10-07.**

## Regeneration after the improvement iteration (2026-10-07)

After the author approved v4, the improvement iteration changed the inputs. The capped fallback added 1,499 classified records, so the top issue now has 59,334 complaints. A new evidence-checker agent screened the memo examples: 48 candidates, 8 rejected, including the paying-user review `d8be33b8`. The memo was regenerated with the same `memo_v4` prompt, so the author's A-D decisions are kept. It passes all automatic checks. **Final status: the author re-read and APPROVED the regenerated memo on 2026-10-07** (`runs/full/memo/human_review.json`). The author also approved the README's Learnings section (§9), which records her reflections.
