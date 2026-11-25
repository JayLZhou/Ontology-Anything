# Review and approval

`build` never produces something an agent can act on. It produces a **draft**, with a status on every item, and `approve` turns the parts you accept into `ontology.yaml` and `seed.json`.

## Item statuses

Each object type, role, action, rule and fact has an id (`rule:cfo_over_30k`, `fact:1`, ...) and one status:

| Status | Shown as | Meaning | On approval |
|---|---|---|---|
| `proposed` | OK | the quote was found verbatim in its source and the item compiles | kept |
| `unverified` | UNVERIFIED | the quote is missing or does not appear in the source document | left out, unless `--include-unverified` |
| `invalid` | INVALID | it does not compile, refers to something that does not exist, or its action is invalid | always left out |

Two checks decide the status:

1. **Quote verification.** The quoted sentence must be a substring of the source document after removing whitespace and normalising full-/half-width characters (NFKC). Quotes shorter than four characters never count. A paraphrase is UNVERIFIED on purpose -- it is how invented rules are caught.
2. **Compilation.** Each action is compiled by nano-onto on its own, then each rule on its own with its action, so one bad rule cannot hide another or invalidate its neighbours. Typical errors: a property that does not exist, an unknown role or object type, a syntax error in the expression.

Facts are checked against the data instead: the record must exist, the properties must exist, values must match the property's type and enum, and the primary key may not be changed. The draft records the value **before** the change so the report can show `blacklisted: False -> True`.

Roles are names rather than claims, so they are kept even without a verified quote. A role that an action or rule uses but that was never described is declared automatically and mentioned in the notes.

## Conflicts

Rules on the same action are grouped when they compare the same expression in the same direction with the same outcome -- for example two rules of the form `params.amount > N` that both require CFO approval.

| Kind | When | Suggestion |
|---|---|---|
| `threshold_mismatch` | different numbers | from the dates, see below |
| `duplicate` | the same number | `keep:<newest>` |

Each rule is dated by the first of: an effective date stated in its own text, the document's "effective from" date, the latest date mentioned in the document. For a threshold mismatch:

- the newest rule's date is **in the future** and every rule has a date → `schedule`
- the newest rule is already in force → `keep:<newest>`
- no dates → no suggestion; a person must decide

"Today" is the real date unless you pass `build --today YYYY-MM-DD`.

### Resolutions

```bash
ontology-anything approve out --resolve C1=keep:cfo_over_30k   # keep one, remove the others
ontology-anything approve out --resolve C1=schedule            # keep all, switch by date
ontology-anything approve out --resolve C1=suggested           # take the suggestion for C1
ontology-anything approve out --accept-suggestions             # take every suggestion
```

`schedule` rewrites the rules' conditions so exactly one is active at a time:

```yaml
- id: cfo_over_50k
  when: (params.amount > 50000) and today() < '2026-12-01'
- id: cfo_over_30k
  when: (params.amount > 30000) and today() >= '2026-12-01'
```

Approval stops with exit code 2 while any conflict is undecided. A conflict whose other rules are being left out anyway (invalid, unverified or dropped) needs no decision, and the surviving rule is kept unchanged. If several rules of a conflict survive, choosing `keep:` for one that is being left out is an error rather than a silent loss of all of them.

## Dropping and including items

```bash
ontology-anything approve out --accept-suggestions \
    --drop rule:warn_rating_c --drop fact:1 \
    --include-unverified
```

`--drop` takes any item id shown in `review.md`; an unknown id is an error. Dropping an action drops its rules. Dropping an object type also removes its records and links from the seed, but if a link type or an action still refers to it the result no longer compiles and approval says so instead of writing anything.

## What approval guarantees

- No INVALID item, and no UNVERIFIED item you did not ask for, is in the output.
- No conflict was resolved without an explicit choice or `--accept-suggestions`.
- The final ontology compiles under nano-onto as a whole; otherwise nothing is written.
- Accepted facts are applied to the seed records.
- Every decision is listed in `approval.log`.

It does **not** guarantee that a verified, compiling rule means what the document means. That is what reading `review.md` is for: each rule is shown next to the sentence it came from.

## In Python

```python
from ontology_anything import ReviewError, approve

try:
    ontology, seed, log = approve(
        draft,
        resolutions={"C1": "schedule"},
        drop=["rule:warn_rating_c"],
        include_unverified=False,
    )
except ReviewError as exc:
    print(exc)   # unresolved conflicts, unknown ids, or an ontology that does not compile
```

`draft.items` maps item ids to `{kind, status, sources, errors}`, `draft.conflicts` lists `{id, kind, action, rules, detail, suggestion, why}`, and `draft.to_dict()` / `Draft.from_dict()` round-trip through `draft.json`.
