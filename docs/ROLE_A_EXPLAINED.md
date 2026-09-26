# Role A, explained simply

Role A builds the **world the agents live in** and the **referee that keeps score**. The other
roles build the agents that learn and fight denials. Without Role A they would have no claims, no
insurers to send them to, and no honest way to know whether they are getting better.

There are three parts, one folder each:

| Folder | Nickname | In one sentence |
|---|---|---|
| `forge/` | the claim factory | makes 2,000 realistic hospital bills from synthetic patients |
| `sim/` | the fake insurers | three insurers that pay or deny bills with secret rules, and judge appeals |
| `scorer/` | the referee | compares what the agents did with the secret truth and posts the score |

---

## 1. The claim factory (`forge/`)

**Where the patients come from.** [Synthea](https://github.com/synthetichealth/synthea) is a free
generator of fake-but-realistic patients. We download its sample (108 patients, 5,571 doctor
visits). Nobody in it is real, so there is no privacy risk in the data itself.

**Turning a visit into a bill.** A hospital bill (a "claim") says *why* the patient came (a
diagnosis code) and *what* was done (procedure codes). We use a small, fixed menu of 40 codes:

- 20 diagnosis codes (ICD-10-CM), for example `I10` high blood pressure, `R07.9` chest pain, `Z09` follow-up visit.
- 20 procedure codes (HCPCS Level II), for example `G0463` clinic visit ($140), `C8901` MRA scan ($2,150), `A0429` ambulance ($650).

For each Synthea visit we pick the diagnosis from the visit reason, and the services from the
visit type (check-up, clinic visit, emergency, hospital stay). Result: `data/forge/claims.jsonl`,
2,000 claims split across the three insurers (689 / 635 / 676).

**The paperwork gap (the thing the agents learn).** Some services need extra paperwork: a *prior
authorization* number (the insurer said yes in advance) or a *referring provider* (which doctor sent
the patient). The hospital almost always *has* this paperwork in its records, but the billing clerk
sometimes forgets to copy it onto the claim. Those claims get denied. A smart agent learns "for this
insurer, always copy the referral onto clinic visits", and the denials stop. That is the learning curve.

**Two special tags.**

- **Holdout (20% of claims).** These are the "exam questions". Acceptance is measured only on
  them, so the score shows how well the agents do on claims they did not learn from.
- **Planted PHI (5% of claims).** PHI is personal health information: names, birth dates, phone
  numbers, social security numbers. On 100 claims we add a realistic clerk's note like
  *"pt called back, DOB 04/12/1961, cell (508) 555-0142"*. It is a trap: the privacy firewall (Role B)
  must stop it before it ever reaches an AI model. We remember what we planted only as scrambled
  fingerprints (hashes), so the database never holds the PHI in readable form.

---

## 2. The fake insurers (`sim/`)

A small web service (port 8001) that pretends to be three insurance companies. You send it a
claim, it answers **paid** or **denied** with the standard industry reason codes (CARC/RARC, for
example `CO-16 / M62` = "missing authorization number"). It never uses AI: every decision comes from
fixed rules, and the same claim always gets the same answer.

### Two kinds of denials

1. **Legitimate denials.** The claim really was wrong (missing paperwork, too many units, a code
   that isn't paid together with another). The right move is to *fix the claim next time*.
2. **Wrongful denials.** The claim was fine and the insurer's own published policy says it is
   covered, but the insurer denied it anyway. The right move is to *appeal*, quoting the evidence.

The agents never see which kind a denial is. Only the simulator knows, and it writes the truth into
a locked database collection called `sim_truth`.

### The three insurers and their personalities

| Insurer | Personality | Its planted wrongful behavior | What wins an appeal |
|---|---|---|---|
| Payer A | strict but fair | denies chronic-condition clinic visits (diabetes, kidney disease, high blood pressure) as "not medically necessary", though its clause 7 covers them | cite the right **clause number** |
| Payer B | algorithmic denier | a robot that denies in bulk within 2 seconds: expensive scans, follow-up visits, and "missing authorization" when the number is right there on the claim | show **2+ similar claims it paid** plus the **denial-pattern numbers** |
| Payer C | policy drifter | denies emergency visits for chest or abdominal pain, against the "prudent layperson" rule in its clause 5 | **quote the clause word for word** from the *current* policy |

A legitimate denial is always upheld on appeal, no matter how good the letter is. So appealing
everything does not work; the agents have to tell the two kinds apart.

### Published policies

Each insurer has a public rulebook with 12 clauses (`sim/policies/*.md`). These are the evidence
the agents search and quote. Every wrongful behavior breaks one of these clauses, so the proof is
always there to be found.

### The policy-change button

Payer C has a second version of its rulebook. Pressing the button (`POST /admin/policy-change/payer_c`)
switches Payer C to version 2: clauses 4 to 7 are rewritten, and the secret rules change with them
(for example, clinic visits now need a referring provider). Acceptance drops, and the audience
watches the agents notice and re-learn. `POST /admin/policy-reset/payer_c` puts it back.

### Stub mode

`python -m sim --stub` answers randomly but in the right format. It existed so the other roles
could start building before the real rules were ready.

---

## 3. The referee (`scorer/`)

Every 30 seconds the scorer looks at the secret truth and at what the agents did, and writes a
scoreboard row per insurer (plus an "all" row) into the `metrics` collection. Role D's live view
draws its charts from these rows.

| Number | Plain meaning |
|---|---|
| acceptance rate | of the last 60 "exam question" claims, how many were paid first time |
| Judge precision | when the Judge said "this denial is wrongful", how often it was right |
| Judge recall | of the truly wrongful denials, how many the Judge caught |
| appeal win rate | how many appeals the insurers overturned |
| recovered $ | money won back through appeals |
| PHI leaks to LLM | planted personal data found in anything the AI wrote. **Must stay 0** |
| leaks blocked | how many times the firewall stopped personal data |

It is the **only** code allowed to read `sim_truth`. If the agents could read it they would be
cheating, and the scoreboard would be meaningless. That is why it is called the *honest* scoreboard.

---

## The numbers we tuned to

Measured with `python -m sim.report` on the 2,000 claims:

| | Payer A | Payer B | Payer C | All |
|---|---|---|---|---|
| Paid at the start (agents know nothing) | 67.5% | 61.6% | 72.9% | **67.5%** |
| Paid once every fixable mistake is learned | 82.9% | 76.2% | 88.8% | **82.8%** |

- Of the denials at the start, about **63% are legitimate** (fixable) and **37% wrongful** (appealable),
  close to the 60/40 target in the plan.
- The climb from about 67% to about 83% is the **learning curve** the demo shows. The rest can't be
  fixed on the bill, because those denials are wrongful and only an appeal gets that money back.
- Pressing Payer C's policy-change button drops it from **88.8% to 70.0%** until the agents re-learn.

---

## How the pieces connect

```
Synthea ──> forge ──> claims (encrypted in MongoDB) ──> orchestrator (Role C)
                                                            │ tokenize (Role B firewall)
                                                            ▼
                                        sim  POST /submit  ──> paid / denied + codes
                                             POST /appeal  ──> overturned / upheld
                                             writes sim_truth (secret)
                                                            │
                          scorer reads sim_truth + agents' verdicts every 30 s
                                                            ▼
                                        metrics ──> live view scoreboard (Role D)
```

## Commands cheat sheet

```bash
python -m forge all            # download Synthea, build 2,000 claims, load into MongoDB
python -m sim.report           # check the denial mix
python -m sim                  # start the insurers on port 8001 (--memory without MongoDB)
python -m scorer               # start the scoreboard (every 30 s)
python -m pytest forge/tests sim/tests scorer/tests
```
