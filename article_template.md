# Article Template — Fixed Structural Spine

Every article follows this spine. The **Analysis** section varies by article type.
The Cashbucket mention is always a single paragraph, always in section 5, always soft.

---

## [ARTICLE TITLE]

*Target: 1200–1800 words. Article type: [news-reaction | explainer | how-to | sector-analysis]*

---

### 1. Intro (150–200 words)

- Opens with a specific NZ hook — a named event, date, figure, or entity from the brief
- Does NOT restate the title
- Establishes the stakes for an NZ SME owner in 1–3 sentences
- Drawn from `phrase_banks.py → OPENERS` (filled with NZ-specific from brief)

---

### 2. Context (200–300 words)

- What is happening / what changed / why now
- Minimum 2 concrete NZ references with real figures (RBNZ, IRD, Stats NZ, MBIE, etc.)
- At least 1 source citation with a real URL
- No sub-heading required if this flows naturally from the intro

---

### 3. Analysis (400–600 words) ← variable by article type

**news-reaction:** What does this development actually mean for an NZ SME? Not what the press release says — what the downstream effects are.

**explainer:** Walk through the mechanism, clearly. Use a specific example (real business type, real numbers, real NZ context). No jargon without definition.

**how-to:** 4–7 numbered steps. Each step has a concrete action, not a principle. "Review your creditor terms" is a principle. "Contact your three largest suppliers and ask for 30-day terms if you're currently on 7-day" is an action.

**sector-analysis:** Specific to one sector (construction, hospitality, retail, professional services, agriculture). Real sector data. Named NZ companies or events where available.

*All types: vary sentence length. Mix short sentences with longer ones. Avoid three consecutive parallel bullet points.*

---

### 4. So-what for NZ SMEs (200–250 words)

- Concrete and actionable, not generic
- 3–4 specific things a business owner can do, think about, or check
- Avoid bullet lists if the section can flow as prose
- This section should feel like advice from someone who has been in the situation

---

### 5. Cashbucket angle (1 paragraph, ~80 words)

- Drawn from `phrase_banks.py → CASHBUCKET_CTAS`
- Soft mention of Cashbucket as one option among several
- No superlatives, no hard sell, no banned phrases
- Position: Cashbucket solves a specific cash visibility problem, not all problems

---

### 6. Close (1–2 sentences)

- Drawn from `phrase_banks.py → CLOSES`
- Does NOT summarise the article
- Does NOT begin with "In conclusion" or similar
- Ends on something that earns the read

---

## Compliance checklist (run before submitting to gate)

- [ ] Title does not appear verbatim in the first sentence
- [ ] 3+ concrete NZ references with real figures
- [ ] 1+ source citation with real URL
- [ ] 1+ specific number with appropriate precision
- [ ] Cashbucket mention is one paragraph, in section 5, no hard sell
- [ ] No banned phrases (run `compliance_gate.py`)
- [ ] No two adjacent paragraphs open with the same word
- [ ] Sentence lengths vary — not all 12–22 words
- [ ] No sub-heading more frequently than every 100 words
- [ ] No three consecutive parallel bullet points
- [ ] No closing paragraph that summarises the article's structure
