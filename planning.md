## planning.md (Final Version)

```markdown
# Provenance Guard — Planning

> This is your design document, written _before_ implementation. Delete this quote block before submitting.

---

## 1. Detection Signals

Your system uses **three distinct signals** — one semantic, two structural — so their blind spots don't overlap.

### Signal 1 — LLM classification (Groq, `llama-3.3-70b-versatile`)

- **Measures:** holistic, semantic judgment of whether the text _reads_ as human or AI — voice, coherence, tonal tells.
- **Output:** a score in `[0, 1]` where higher = more likely AI. (Prompt the model to return a number or a structured JSON verdict; parse it.)
- **Why it differs between human/AI:** AI prose is relentlessly coherent and hedged; human writing wanders, takes risks, leaves rough edges.
- **Blind spot:** non-deterministic black box; fooled by lightly-edited AI text.

### Signal 2 — Stylometric heuristics (pure Python)

- **Measures:** statistical _uniformity_ of the text via 3 sub-metrics:
  - Sentence-length variance ("burstiness") — humans mix short & long; AI is even.
  - Type-token ratio (unique words ÷ total) — vocabulary diversity.
  - Punctuation density / variety — humans use more varied punctuation.
- **Output:** each sub-metric normalized to `[0, 1]`, combined into one signal score in `[0, 1]` where higher = more likely AI.
- **Why it differs:** AI text is statistically more uniform than human writing.
- **Blind spot:** confuses "formal and even" with "AI" — flags careful non-native writers and polished technical prose. This is the main false-positive risk.

### Signal 3 — Readability/Flesch-Kincaid Score (text complexity)

- **Measures:** text complexity, specifically grade-level readability.
- **Output:** normalized score in `[0, 1]` where higher = more likely AI.
- **Why it differs:** AI-generated text often has consistent, moderately complex readability, while human writing swings from very simple to very complex.
- **Blind spot:** Technical writing and formal reports may have consistent readability, causing false flags.

### Combining the three signals

I use a weighted average to combine the three signals, with the LLM as the primary signal and the two structural signals as secondary anchors:
`combined = 0.5 * llm_score + 0.3 * stylometric_score + 0.2 * readability_score`.

The LLM receives the most weight because semantic understanding is the strongest indicator of human authorship. The stylometric score acts as a guard against the LLM being "talked past" by well-crafted AI text. The readability score provides an additional check, ensuring that text that is too consistent in complexity is flagged.

---

## 2. Uncertainty Representation

- What a combined score of **0.6** means to my system: The text shows moderate evidence of AI authorship, but not enough for a definitive label. It could be highly formal human writing or lightly polished AI text. The system will return the "Uncertain" label for scores between 0.35 and 0.75.

- How raw signal outputs map to a calibrated score: Each signal output is already normalized to `[0, 1]`. The weighted average produces the combined score, which is then mapped to bands. I validate this by testing known human and AI texts to ensure the score ranges are meaningful.

- **Thresholds** (three bands):
  - `score < 0.35` → **likely human** (wide band to minimize false positives)
  - `0.35 ≤ score < 0.75` → **uncertain** (wide band to avoid forced guesses)
  - `score ≥ 0.75` → **likely AI** (narrow band requiring strong evidence)

_(Design note: making the "uncertain" band wide and the "likely AI" band narrow & high encodes the false-positive asymmetry — we need stronger evidence to call something AI than to call it human.)_

---

## 3. Transparency Label Design

Three variants, written in plain language for a non-technical reader. Draft below, then run the "show it to someone who hasn't seen the project" test from the spec.

- **High-confidence HUMAN:**

  > "This text was very likely written by a human. Our analysis found the natural variation and creativity typical of human authorship."

- **High-confidence AI:**

  > "This text was very likely written by an AI. If you're the creator and disagree, you can appeal this result for a human review."

- **UNCERTAIN:**
  > "We could not determine with high confidence whether this text was written by a human or an AI. The evidence was mixed. Creators can request a review if they believe this is incorrect."

---

## 4. Appeals Workflow

- **Who can appeal:** the creator of the content (identified by `creator_id`).
- **What they provide:** the `content_id` and free-text `creator_reasoning`.
- **What the system does on receipt:**
  1. Update that content's status → `"under_review"`.
  2. Log the appeal _alongside_ the original classification in the audit log.
  3. Return a confirmation.
- **No automated re-classification** (not required).
- **What a human reviewer would see in the queue:**
  The reviewer's queue would display:
  - Original classification (Human/AI/Uncertain)
  - All three individual signal scores (LLM, stylometric, readability)
  - Combined confidence score
  - The full original text
  - The creator's reasoning for the appeal
  - Timestamp of original submission and appeal
    This side-by-side view allows the reviewer to quickly assess whether the original classification was likely correct or not.

---

## 5. Anticipated Edge Cases

1. **Repetitive/simple-vocabulary poetry** — low type-token ratio + low sentence variance + consistent readability → stylometry and readability both score it as AI, though it's a deliberate human style. The LLM may or may not rescue it.

2. **Polished non-native English prose** — formal and uniform → stylometry false-flags; readability may also flag due to consistent complexity; the LLM is the only signal that might recognize the human
```

## Architecture

### Submission Flow

```
                        ┌─────────────────────────────────────────┐
   RAW TEXT ───────────▶│               POST /submit               │
   + creator_id         └─────────────────────┬───────────────────┘
                                               │ raw text
                        ┌──────────────────────┼──────────────────────┐
                        ▼                      ▼                       ▼
                ┌───────────────┐     ┌────────────────┐     ┌────────────────┐
                │  Signal 1     │     │  Signal 2      │     │  Signal 3      │
                │  LLM (Groq)   │     │  Stylometric   │     │  Readability   │
                │  semantic     │     │  structural    │     │  complexity    │
                └───────┬───────┘     └───────┬────────┘     └───────┬────────┘
                        │ llm_score           │ sty_score            │ read_score
                        │ (0–1)               │ (0–1)                │ (0–1)
                        └──────────────────────┼──────────────────────┘
                                               ▼
                        ┌─────────────────────────────────────────────┐
                        │        Confidence Scoring (weighted)         │
                        │  0.5·llm + 0.3·sty + 0.2·read = combined     │
                        └─────────────────────┬───────────────────────┘
                                               │ combined_score (0–1)
                                               ▼
                        ┌─────────────────────────────────────────────┐
                        │              Label Generator                 │
                        │   < 0.35        →  LIKELY HUMAN               │
                        │   0.35 – 0.75   →  UNCERTAIN                  │
                        │   ≥ 0.75        →  LIKELY AI                  │
                        └─────────────────────┬───────────────────────┘
                                               │ attribution + label_text
                        ┌──────────────────────┼──────────────────────┐
                        ▼                                              ▼
                ┌───────────────┐                            ┌────────────────┐
                │   Audit Log   │◀── decision record ───────▶│    Response    │
                │   (SQLite)    │   (all scores, label)      │    Builder     │
                └───────────────┘                            └───────┬────────┘
                                                                     │
                                                                     ▼
                                                        Response to Client (JSON)
                                                        { content_id, attribution,
                                                          confidence, label, scores }
```

### Appeal Flow

```
   Client
     │  POST /appeal { content_id, creator_reasoning }
     ▼
┌─────────────────────┐   not found   ┌──────────────────────┐
│  Lookup content by  │──────────────▶│  404: unknown         │
│     content_id      │               │  content_id           │
└──────────┬──────────┘               └──────────────────────┘
           │ found
           ▼
┌─────────────────────────────┐
│  Update status → under_review│
│  Store creator_reasoning     │
└──────────┬──────────────────┘
           ▼
┌─────────────────────────────────────────────┐
│  Audit Log: append appeal beside original    │
│  decision (scores + reasoning side by side)  │
└──────────┬──────────────────────────────────┘
           ▼
   Confirmation to Client (JSON)
   { content_id, status: "under_review", message }
```

**Narrative:** A submission's raw text is scored independently by the LLM, stylometric, and readability signals; the three scores are combined by a weighted average into one calibrated confidence value, which selects one of three transparency labels. The full decision (all three signal scores, the combined score, and the label) is written to the SQLite audit log and returned to the client. An appeal looks the content up by `content_id`, flips its status to `under_review`, appends the appeal to the audit log next to the original decision, and confirms receipt.

## AI Tool Plan

- **M3 (submission endpoint + signal 1):**
  - Provide: §1 Detection Signals + the Architecture diagram.
  - Ask for: Flask skeleton with the `POST /submit` route stub + the Groq LLM
    signal function.
  - Verify: call the signal function directly on 3–4 inputs; confirm it returns
    a float in [0,1] and that the route matches the API contract before wiring in.

- **M4 (signals 2 & 3 + confidence scoring):**
  - Provide: §1 + §2 Uncertainty Representation + the diagram.
  - Ask for: the stylometric function, the readability function, and the
    combining logic (0.5·llm + 0.3·sty + 0.2·read).
  - Verify: run the 4 spec test inputs; confirm scores vary meaningfully and that
    the thresholds implemented match §2 exactly (0.35 / 0.75) — AI tools often
    drift from specified ranges, so check the numbers, not just that it runs.

- **M5 (production layer):**
  - Provide: §3 Labels + §4 Appeals + the diagram.
  - Ask for: label-generation function mapping score→variant, and the
    `POST /appeal` endpoint.
  - Verify: all three labels are reachable with crafted inputs; an appeal flips
    status to under_review and writes the appeal beside the original decision.
