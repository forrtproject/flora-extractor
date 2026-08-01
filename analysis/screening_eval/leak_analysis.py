"""Diagnose *why* hard true negatives escape the v3 discard gate, and write leak_analysis.md.

Offline only: reads the `voter_v3_*` result files and the case/truth files already in this
directory. No API calls.

`gate_sweep.py` answers which gate leaks least; this script answers what the leaks are made
of. It takes the 74 hard true negatives (human + held-out, truth = no), keeps every case
where at least one of the three v3 models did not answer `none`, and reports each model's
own verdict, categories, evidence quote and reasoning sentence alongside the abstract, so a
reader can check the taxonomy below against the text.

The taxonomy in PATTERNS was assigned by reading every case's title, abstract and the three
models' reasoning; it is hand-coded here rather than computed, and the cases flagged
`arguable` are ones where the truth label itself is defensible either way (mostly because
v3's Rule 4 on measurement re-validation and its technical-reproducibility exclusion pull in
opposite directions on the same abstract).

Everything here is derivation data (see README.md), so every number is in-sample.
"""
import itertools
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent

FL = "gemini-3.5-flash-lite"
GPT = "gpt-5.4-mini"
MIN = "mistralai/ministral-14b-2512"
MODELS = [FL, GPT, MIN]
SHORT = {FL: "flash-lite", GPT: "gpt-5.4-mini", MIN: "ministral-14b"}

MAP = {"replication": "yes", "reproduction": "yes", "both": "yes",
       "none": "no", "unclear": "unclear"}

SETS = {"human": ("human_cases.json", "human_truth_revised.json"),
        "heldout": ("heldout_cases.json", "heldout_truth.json")}

# ------------------------------------------------------------------ taxonomy
# key -> (name, one-line definition, which v3 rule should have caught it)
PATTERNS: dict[str, tuple[str, str, str]] = {
    "P1": ("Internal two-stage discovery/replication",
           "The paper finds a signal in its own first sample and confirms it in its own "
           "second sample, cohort or stage; the abstract calls that second sample a "
           "'replication set/sample/cohort' or 'stage 2'.",
           "WHAT DOES NOT QUALIFY -> Internal replication ('The target must be earlier "
           "published research in some paper other than this one')."),
    "P2": ("Internal replication across studies in one paper",
           "A study inside this same paper, thesis or dissertation replicates another study "
           "inside it, and the authors use the word 'replication' for it.",
           "WHAT DOES NOT QUALIFY -> Internal replication ('A paper whose Study 2 "
           "replicates its own Study 1 does not qualify')."),
    "P3": ("Tool/model benchmarking against published results",
           "A new model, simulation, numerical method, assay or apparatus is shown to "
           "recover results already reported in the literature, in order to show the tool "
           "works.",
           "Non-qualifying sense 2 ('Tool benchmarking: a new model, simulation or "
           "numerical method demonstrated to reproduce known results')."),
    "P4": ("Inter-laboratory / inter-rater / technical reproducibility of a procedure",
           "Round-robin, ring-trial, proficiency-testing, multi-device or multi-observer "
           "studies that quantify the spread of a measurement procedure.",
           "Non-qualifying sense 1 ('sample-to-sample precision, device-to-device "
           "precision, inter-rater agreement, test-retest agreement') — but v3 Rule 4 "
           "pulls the other way, see the conflict note."),
    "P5": ("Translation / first application of a published instrument to a new setting",
           "An already-published instrument, atlas or assay is translated, culturally "
           "adapted, or applied for the first time to a new population, species or device, "
           "and its properties are established there.",
           "WHAT DOES NOT QUALIFY -> First validation of a new instrument, read together "
           "with Rule 4 — the two do not settle the translated-instrument case."),
    "P6": ("Incidental agreement with prior work credited as an aim",
           "One clause in the results says the paper reproduced or replicated something "
           "earlier, while the paper's stated objectives are about something else.",
           "WHAT DOES NOT QUALIFY -> Declared intent ('Return \"none\" when the abstract "
           "presents the agreement or re-test as an incidental result')."),
    "P7": ("Vocabulary misfire: framework reuse, deployment, biology",
           "'Replication' means reusing an earlier paper's item set or framework as this "
           "study's instrument, rolling an engineering solution out to another site, or "
           "viral/cell/histological replication.",
           "Non-qualifying sense 3 (ordinary-language, biological and field-specific "
           "senses) and Target specificity."),
}

# case id -> (pattern key, arguable?, one-line note)
CASE_PATTERN: dict[str, tuple[str, bool, str]] = {
    # P1 — internal two-stage
    "HU21": ("P1", False, "Polyneuro score built in ABCD, then tested in the Oregon cohort — "
                          "internal validation of a score this paper constructs."),
    "HU27": ("P1", False, "GWAS discovery set of 458 patients, 'replication of the top SNPs' "
                          "in a further 185 of the same study."),
    "HU51": ("P1", True, "Discovery WES sample replicated with imputed data, but the abstract "
                         "also says it replicates 'dozens of previously reported genes' — the "
                         "external half is real, so the `no` label is defensible but not obvious."),
    "H005": ("P1", False, "GWAS with 'replication of promising signals in an independent sample "
                          "who completed the program at a later date' — the same program."),
    "H011": ("P1", False, "A new Methylation Index is developed, then validated and "
                          "'replicated in a meta-analysis' of the project's own cohorts."),
    "H012": ("P1", False, "Explicit two-stage GWAS: 'selected for replication (stage 2)'."),
    "H021": ("P1", False, "Prognostic models proposed here; 'reproducibility of prognostic "
                          "results was confirmed for new ACS cases' is internal validation."),
    "H028": ("P1", False, "Same paper as HU27 (the case appears in both sets)."),
    # P2 — internal replication across studies in one paper
    "HU05": ("P2", False, "'two separate experimental studies conducted, a replication of one "
                          "another' — both are in this paper."),
    "HU08": ("P2", False, "Dissertation whose 'second objective pertains to the replication of "
                          "these combinations' found in its own first study."),
    "HU34": ("P2", False, "'a replication of Study 2 (Study 6) was conducted' — Study 2 is in "
                          "this same paper."),
    "HU41": ("P2", False, "eQTL are mapped here in RILs, then 'independent replication' is "
                          "tested with introgression lines of the same project."),
    # P3 — tool/model benchmarking
    "HU07": ("P3", True, "Least-squares method 'extended to reproduce the results of "
                         "perturbation studies' — the target is a published empirical estimate, "
                         "so reading this as a computational re-test is defensible."),
    "HU28": ("P3", True, "Monte Carlo experiments 'to test the effectiveness of Carr and Lee's "
                         "immunization strategy' — evaluating a published method, not a "
                         "reported empirical finding, but the boundary is thin."),
    "HU33": ("P3", False, "Eurocode load models assessed for whether they 'adequately reproduce "
                          "the effects caused by LHVs' — model adequacy, not a prior finding."),
    "HU39": ("P3", False, "New VR/haptic rig built 'to reproduce the effect of prism adaptation' "
                          "as a demonstration that the rig works."),
    "HU48": ("P3", False, "Simulation environment 'allowing the replication of already reported "
                          "experimental findings' — textbook tool benchmark."),
    "HU53": ("P3", False, "'The numerical model developed in previous studies has been verified "
                          "in the laboratory' — model verification."),
    "HU59": ("P3", False, "'We reproduce results from previous modelling studies' as a "
                          "sanity-check inside a new numerical study."),
    "HU60": ("P3", False, "Overset grid method 'assessed to benchmark cases'; 'the simulations "
                          "reproduced results from the literature at a significantly reduced cost'."),
    # P4 — technical / inter-lab reproducibility
    "HU02": ("P4", False, "'sample-to-sample reproducibility of the electronic properties and "
                          "device performance' — device-to-device precision."),
    "HU09": ("P4", False, "'demonstrate the anti-fibrotic reproducibility' of a control dose "
                          "across the authors' own four studies."),
    "HU18": ("P4", False, "NIST/NREL inter-laboratory comparison of high-throughput methods."),
    "HU40": ("P4", False, "Protocol for a quality-assessment programme measuring inter-pathologist "
                          "reproducibility of a scoring method the authors developed."),
    "HU46": ("P4", True, "Inter-laboratory programmes 'verifying specific standard test methods' "
                         "— the standards' published limits are the target, so arguable."),
    "H001": ("P4", False, "Proficiency-testing panel distributed to ten labs to evaluate assay "
                          "performance."),
    "H008": ("P4", True, "CPX round-robin across four trailers; the abstract names a prior "
                         "Netherlands trailer comparison it extends, so arguable."),
    # P5 — translation / first application to a new setting
    "HU17": ("P5", False, "OECD No. 202 applied to a new test organism, establishing its "
                          "metrological characteristics there for the first time."),
    "HU58": ("P5", True, "Reliability and validity of a published ultrasound system re-examined "
                         "in team-sport athletes — v3 Rule 4 arguably makes this qualify."),
    "H003": ("P5", True, "Psychometrics of a Persian version of a published inventory — Rule 4 "
                         "says re-validation 'in a new population, language or setting' qualifies."),
    "H006": ("P5", True, "Preliminary adaptation of the CAPI to Venezuela — same Rule 4 tension."),
    "H014": ("P5", True, "'only one paper evaluated the recovery rate of GIN eggs by Mini-FLOTAC "
                         "… To further study' — this names the single prior paper it extends, "
                         "which is close to a declared re-test."),
    "H026": ("P5", True, "'assess the applicability and reproducibility of the GP atlas' in a new "
                         "regional population — Rule 4 arguably makes this qualify."),
    # P6 — incidental agreement
    "HU01": ("P6", False, "'We reproduced findings from rodent neurons' is one result inside a "
                          "cell-line characterisation paper."),
    "HU12": ("P6", True, "The authors' own earlier dynamic connectivity states are re-found 'in "
                         "an independent dataset' — a defensible self-replication."),
    "HU35": ("P6", False, "'In previous studies, we were able to demonstrate…' is background; the "
                          "paper compares experimental platforms."),
    "HU45": ("P6", False, "'replicating previous evidence of executive dysfunction in HD' is a "
                          "sub-clause in a myelin-imaging paper."),
    # P7 — vocabulary misfire
    "HU44": ("P7", False, "'using 16 elements of replication of Ramirez and Gordillo's research' "
                          "means reusing their element list as this study's instrument."),
    "HU54": ("P7", False, "'we observed the replication of Zamilon' is viral replication."),
    "H027": ("P7", False, "'Revisiting the Mississippi Fan' tests long-held conventions of a "
                          "field, i.e. accepted background knowledge, not a reported finding."),
}

# ------------------------------------------------------------------ proxy pool split
# id -> (bucket a/b/c, pattern key or "", note)
PROXY_SPLIT: dict[str, tuple[str, str, str]] = {
    "N002": ("a", "", "'I aimed to provide a conceptual replication and extension of previous "
                      "findings' (Jusyte et al., 2014) — a genuine conceptual replication."),
    "N012": ("a", "", "Title and abstract both say 'a conceptual replication of Elbro et al. "
                      "(2012a)'."),
    "N013": ("a", "", "'The present study, as a conceptual replication of Moir and Nation (2002)'."),
    "N022": ("a", "", "'This analysis serves as a replication of Steimer & Mata (2016).'"),
    "N028": ("a", "", "'Here, we replicate and extend this finding' (Filmer et al., 2015). The "
                      "record itself is a peer-review report of that paper, so the row is a "
                      "record-type problem, not a screening error."),
    "N017": ("b", "P6", "A 1988 finding is contradicted in passing in a paper whose aim is to "
                        "build an assay cell line."),
    "N034": ("b", "P5", "Cross-cultural adaptation of a voiding score to Serbian."),
    "N035": ("b", "P5", "Reliability of an already-adapted play scale for Brazilian children."),
    "N036": ("b", "P3", "'demonstrating the reproducibility of the Mr.MAPP framework' the authors "
                        "built, then extending it."),
    "N037": ("b", "P5", "Spanish adaptation and validation of a self-care questionnaire."),
    "N041": ("b", "P4", "ESIS TC4 multi-laboratory round-robin on a testing scheme."),
    "N052": ("b", "P1", "ADNI-1 discovery, ADNI-GO-2 confirmation inside one analysis."),
    "N058": ("b", "P3", "Numerical model 'compared with the previous work to verify the "
                        "reproducibility of the experimental results'."),
    "N113": ("b", "P7", "A dataset deposit whose abstract says it is 'provided for replication of "
                        "results' — a data artefact, not a study."),
    "N114": ("b", "P7", "'histological replication of GP on mouse skin' — reproducing a disease "
                        "phenotype in a model, ordinary-language sense."),
    "N120": ("b", "P5", "Reliability and validity of a Chinese version of a checklist."),
    "N122": ("b", "P5", "Psychometrics of a Turkish C-OIDP."),
    "N125": ("b", "P4", "Collaborative-study statistics on repeatability and reproducibility of "
                        "two GC column types."),
    "N126": ("b", "P4", "Two hip-distraction devices compared for measurement agreement."),
    "N127": ("b", "P4", "Inter- and intra-observer concordance of a grading system."),
    "N133": ("b", "P7", "'replication of the solution in the Indian context' — rolling an "
                        "engineering pilot out to another site."),
    "N146": ("b", "P4", "An image-derived input function validated against a reference standard."),
    "N147": ("b", "P4", "Inter-laboratory ring trial of lipidome profiling."),
    "N148": ("b", "P1", "'the reproducibility of the prediction model was validated using "
                        "independent test data from another site' — the model is built here."),
    "N150": ("b", "P3", "A computational model previously used for yellow fever is shown to "
                        "reproduce ChAdOx1 antibody curves — tool transfer."),
    "N119": ("c", "", "'few studies have reproduced the results on ecommerce platforms' — a "
                      "context transfer of country-of-origin effects, but the target is a "
                      "literature-wide effect rather than one reported finding."),
    "N121": ("c", "", "'we have reproduced the original results from the TEXTOR tokamak' — a real "
                      "re-test in a new device, but incidental to a thesis on PDIs."),
    "N124": ("c", "", "Two-sentence abstract on 'the reproducibility of modified Bosniak "
                      "classification'; flash-lite itself answered unclear@low."),
    "N149": ("c", "", "An already-published solubility model is re-run on the second Solubility "
                      "Challenge set — external validation of the authors' own model."),
}

# ------------------------------------------------------------------ proposals
# (id, pattern keys, title, scope, draft, placement, recall risk, risk level)
PROPOSALS = [
    ("F4", ["P2"], "Generalise the internal-replication rule beyond 'Study 2 replicates Study 1'",
     "SHARED. The pattern is close to flash-lite/ministral-specific — gpt-5.4-mini gets all but "
     "one of the cluster right — but a one-case exposure is enough to keep the segment shared "
     "rather than split the prompt",
     "Replace the *Internal replication* bullet with: \"Internal replication. A re-test of a "
     "result obtained elsewhere in this same paper, thesis or dissertation does not qualify, "
     "whatever the authors call it — 'Study 6 was a replication of Study 2', 'two experiments "
     "that are a replication of one another', or a second objective that replicates a pattern "
     "the paper itself has just reported. The target must be earlier published research in some "
     "paper other than this one.\" Add to qualifying rule 6: \"Author self-declaration applies "
     "only when the declared target is earlier published research in another paper; a declared "
     "'replication' whose target is elsewhere in this same paper is an internal replication.\"",
     "`WHAT DOES NOT QUALIFY`, replacing the existing *Internal replication* bullet (line 78), "
     "plus one clause on qualifying rule 6 (line 68).",
     "Near zero. The amendment only names targets that are inside the paper being screened, "
     "which no FLoRA positive can be: every positive's target is another paper. The one live "
     "risk is a multi-study paper whose Study 3 replicates an *external* study — the wording "
     "'obtained elsewhere in this same paper' leaves that untouched.",
     "very low"),
    ("F1", ["P1"], "Name the discovery/replication two-stage design as internal",
     "SHARED. Every model is exposed, so no single-model segment reaches the cluster",
     "Add to the *Internal replication* bullet: \"This covers the two-stage discovery design: a "
     "study that identifies a signal in its own discovery sample and then confirms it in its own "
     "second sample does not qualify, however that second sample is labelled. 'Replication set', "
     "'replication sample', 'replication cohort', 'stage 2' and 'confirmed in an independent "
     "cohort' describe a design internal to the paper, not a check on another paper's finding. "
     "It qualifies only if the abstract says the association being confirmed was reported by "
     "earlier research.\"",
     "`WHAT DOES NOT QUALIFY`, immediately after the *Internal replication* bullet (line 78).",
     "Genuine but bounded. FLoRA contains real external genetic replications ('we sought to "
     "replicate the association reported by Smith et al. in an independent cohort'), and the "
     "amendment's cue words are exactly the ones those abstracts use. The final sentence is the "
     "guard: it re-admits any abstract that attributes the signal to earlier research. Expect a "
     "small number of terse GWAS abstracts that never name a source to move from qualifying to "
     "`none`.",
     "low-moderate"),
    ("F3a", ["P4"], "Make round-robin / inter-rater studies an explicit instance of technical "
                    "reproducibility",
     "SHARED. flash-lite is the biggest offender here, not ministral-14b, so this is the one "
     "cluster a ministral-only segment would clearly fail to fix",
     "Extend non-qualifying sense 1 to: \"1. Technical or measurement reproducibility of a "
     "procedure — sample-to-sample precision, device-to-device precision, inter-rater and "
     "intra-rater agreement, test-retest agreement, and multi-laboratory round-robin, ring-trial "
     "or proficiency-testing exercises. The aim of these studies is to quantify the spread of a "
     "measurement, not to check a number an earlier paper reported. It does qualify when the "
     "paper aims to replicate previously published estimates of those properties.\"",
     "`WHAT DOES NOT QUALIFY`, rewriting numbered sense 1 (line 82).",
     "Low, but not nil, and it is in tension with Rule 4. A FLoRA positive that re-runs a "
     "published inter-rater reliability study and compares its coefficient with the published one "
     "is protected by the retained final sentence; one that simply re-measures reliability in a "
     "new lab is not, and would be discarded.",
     "low"),
    ("F6", ["P7"], "Widen the ordinary-language sense to framework reuse and deployment",
     "SHARED. Only three cases, one per model in effect — too thin to justify a per-model split",
     "Extend non-qualifying sense 3 to: \"3. Ordinary-language, biological and field-specific "
     "senses: DNA, viral, cell or histological replication; 'replication' as a count of "
     "overlapping samples in a chronology; 'replicated across sites' describing the internal "
     "design of the study being reported; reuse of an earlier paper's framework, element list or "
     "protocol as this study's instrument ('using 16 elements of X's research'); and rolling an "
     "engineering solution, pilot or intervention out to a further site.\"",
     "`WHAT DOES NOT QUALIFY`, rewriting numbered sense 3 (line 84).",
     "Near zero for the biological and deployment clauses. The framework-reuse clause carries "
     "a little risk: a paper that reuses an original's instrument *in order to re-test its "
     "finding* is a genuine conceptual replication, and the clause must not catch it — the "
     "wording 'as this study's instrument' rather than 'to re-test the original claim' is what "
     "keeps them apart.",
     "very low"),
    ("F2", ["P3"], "Close the tool-benchmark loophole opened by the words 'reproduce' and 'verify'",
     "SHARED, but justified mainly by ministral-14b, which is the only model badly exposed here. "
     "A ministral-only segment would fix most of this cluster on its own; it is proposed as "
     "shared because it is a clarification of a rule the prompt already states, so it cannot "
     "hurt a model that already applies it, and shared text keeps one cache key and one prompt "
     "to maintain",
     "Extend non-qualifying sense 2 to: \"2. Tool benchmarking: a new model, simulation, "
     "numerical method, assay or apparatus demonstrated to reproduce known results in order to "
     "show that the tool works. This holds even when the abstract names the published results "
     "the tool recovers, and even when it uses the words 'reproduce', 'replicate', 'verify' or "
     "'validate against' — recovering what earlier work reported is a property of the tool, and "
     "the tool is the paper's subject. Exception: a study whose aim is to settle whether the "
     "earlier claim itself holds, including a re-analysis of the earlier study's own data, "
     "qualifies.\"",
     "`WHAT DOES NOT QUALIFY`, rewriting numbered sense 2 (line 83).",
     "Moderate, and concentrated on reproductions. Computational reproductions — re-running an "
     "original's analysis in new code and reporting whether the numbers come out — read very "
     "much like 'our implementation reproduces the published results', which is precisely what "
     "this amendment tells the model to discard. The exception clause is doing all the work and "
     "must name re-analysis of the original data explicitly, as drafted. This is the proposal "
     "most likely to cost recall on the `reproduction` half of the database and the one that "
     "most needs a check against the FLoRA positive set before shipping.",
     "moderate"),
    ("F5", ["P6"], "Make 'the aim, not a result' operational",
     "SHARED, with the caveat in the per-model note below — gpt-5.4-mini is already the model "
     "that applies this rule correctly",
     "Add under *Declared intent*: \"A single clause in the results is not a declared aim. "
     "'We reproduced findings from X', 'replicating previous evidence of Y', 'consistent with "
     "previous studies' and 'in line with earlier reports' are interpretive remarks. Read the "
     "abstract's stated objectives: if the check does not appear among them, answer 'none' even "
     "though the agreement is real.\"",
     "`WHAT DOES NOT QUALIFY`, appended to the *Declared intent* bullet (line 74).",
     "Moderate. Some genuine replications bury the declaration in the results rather than the "
     "objectives, particularly short or structured abstracts where the objectives sentence is "
     "about the broader study. The instruction to weigh the objectives section over a results "
     "clause is a real recall trade, and the FLoRA positives should be re-scored under it before "
     "it ships.",
     "moderate"),
    ("F3-full", ["P4", "P5"], "Rewrite Rule 4 to draw the measurement-re-validation line "
                              "(NOT RECOMMENDED as drafted)",
     "SHARED — every model falls for this cluster, and it is the largest single group of leaks",
     "Replace qualifying rule 4 with: \"4. Measurement re-validation. Re-testing an already "
     "published instrument, scale, test or clinical procedure against a performance figure "
     "earlier research reported for it qualifies — for example checking whether a published "
     "scale's reported factor structure, or a published method's reported accuracy, holds in a "
     "new population or language. It does not qualify when the study is establishing that "
     "instrument's properties in this setting for the first time — a translation, a cultural "
     "adaptation, or a first application to a new species, device or population — because there "
     "is no earlier reported figure for this setting to check.\"",
     "`WHAT QUALIFIES`, replacing numbered rule 4 (line 66).",
     "**High, and pointed at exactly the positives the coding rules were changed to protect.** "
     "`README.md` records that the settled labelling flipped seven cases to `yes` under the rule "
     "that re-validation of a published instrument counts, one of them a Japanese ecSI-2.0 "
     "translation (case 37 — the single wrongly-discarded row in block B). This amendment would "
     "re-discard that class. It is written out here because it is the only amendment that "
     "reaches the largest cluster, and because the cluster is as much a truth-label problem as a "
     "model problem — but it should not ship without re-labelling the translated-instrument "
     "cases first.",
     "high"),
]


def load_cases(name: str) -> dict[str, dict]:
    return {c["id"]: c for c in json.loads((HERE / name).read_text())}


def read_v3(model: str, stem: str) -> dict[str, dict]:
    f = HERE / f"voter_v3_{model.replace('/', '_')}_{stem}.json"
    out = {}
    for r in json.loads(f.read_text()):
        bad = bool(r.get("schema_error")) or bool(r.get("error"))
        out[r["id"]] = {"cls": r.get("classification"),
                        "verdict": None if bad else MAP.get(r.get("classification") or ""),
                        "conf": r.get("confidence"), "cats": r.get("categories") or [],
                        "ev": r.get("evidence_quote") or "", "reason": r.get("reasoning") or "",
                        "bad": bad}
    return out


def esc(s: Optional[str], n: int = 0) -> str:
    s = (s or "").replace("\n", " ").replace("|", "/").strip()
    return s[:n] + ("…" if n and len(s) > n else "")


def main() -> None:
    cases = {s: load_cases(cf) for s, (cf, _) in SETS.items()}
    truth = {s: json.loads((HERE / tf).read_text())["truth"] for s, (_, tf) in SETS.items()}
    data = {(m, s): read_v3(m, s) for m in MODELS for s in SETS}

    hard_neg = [(s, i) for s in SETS for i in cases[s] if truth[s].get(i) == "no"]
    leaked = [(s, i) for s, i in hard_neg
              if any(data[(m, s)].get(i, {}).get("verdict") != "no" for m in MODELS)]
    wrong = {m: {(s, i) for s, i in hard_neg if data[(m, s)].get(i, {}).get("verdict") != "no"}
             for m in MODELS}

    L: list[str] = ["# Leak analysis — why hard true negatives survive the v3 discard gate", ""]
    L.append("Generated by `leak_analysis.py` from the `voter_v3_*` result files in this "
             "directory. No API calls. Negatives: the 74 truth-`no` cases of "
             "`human_cases.json` + `human_truth_revised.json` (50) and `heldout_cases.json` + "
             "`heldout_truth.json` (24). Three models were run on all of them with "
             "`prompt_v3.txt`: flash-lite, gpt-5.4-mini and ministral-14b.")
    L.append("")
    L.append(f"**{len(leaked)} of the {len(hard_neg)} hard negatives have at least one model "
             f"answering something other than `none`.** Under the best gate in `gate_sweep.md` "
             "(2 of 3 say `none`, at least one at high confidence) about 25 of them still "
             "survive; the wider set of 40 is the pool that any prompt fix has to shrink, "
             "because a gate can only work with the verdicts it is given.")
    L.append("")
    L.append("These cases are derivation data (see `README.md`), so every number here is "
             "in-sample.")
    L.append("")
    L.append("**Headline.** The leaks are not scattered: seven patterns account for all 40, and "
             "five of the seven are cases the v3 prompt already has a rule against — the rules "
             "are being out-argued by a single sentence in the abstract, not omitted. The "
             "largest cluster (13 of 40, patterns P4+P5) is different in kind: there the prompt "
             "contradicts itself, because qualifying rule 4 says re-validating a published "
             "instrument \"in a new population, language or setting\" qualifies while "
             "non-qualifying sense 1 says inter-rater and test-retest reproducibility does not, "
             "and a translated-questionnaire abstract satisfies both. Eleven of the 40 are "
             "flagged below as cases where the `no` label itself is arguable.")
    L.append("")

    # ------------------------------------------------------------ 1. case table
    L += ["## 1. Case-level error table", "",
          "Every hard negative where at least one model did not answer `none`, with each "
          "model's verdict, confidence, categories, evidence quote and its own reasoning "
          "sentence. Verdicts that are not `none` are **bold**. Abstracts were read from the "
          "case JSONs; the `pattern` column is the taxonomy of section 2.", ""]
    for s, i in leaked:
        c = cases[s][i]
        pk, arg, note = CASE_PATTERN[i]
        L.append(f"### {s}:{i} — {esc(c['title'], 160)}")
        L.append("")
        L.append(f"*Pattern {pk} ({PATTERNS[pk][0]})"
                 + (" — **truth label arguable**" if arg else "") + f". {note}*")
        L.append("")
        L += ["| model | verdict | conf | categories | evidence quote | reasoning |",
              "| --- | --- | --- | --- | --- | --- |"]
        for m in MODELS:
            r = data[(m, s)][i]
            v = r["cls"] or "(no answer)"
            v = f"**{v}**" if r["verdict"] != "no" else v
            L.append(f"| {SHORT[m]} | {v} | {r['conf']} | {', '.join(r['cats'])} "
                     f"| {esc(r['ev'], 150)} | {esc(r['reason'], 260)} |")
        L.append("")

    # ------------------------------------------------------------ 2. taxonomy
    by_pat: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for s, i in leaked:
        by_pat[CASE_PATTERN[i][0]].append((s, i))

    L += ["## 2. Pattern taxonomy", "",
          "| pattern | n | share of 40 | case ids | models that fall for it (n of the cluster) |",
          "| --- | --- | --- | --- | --- |"]
    for pk in PATTERNS:
        ids = by_pat[pk]
        who = ", ".join(f"{SHORT[m]} {sum(1 for si in ids if si in wrong[m])}" for m in MODELS)
        L.append(f"| **{pk}** {PATTERNS[pk][0]} | {len(ids)} | {len(ids) / len(leaked):.0%} "
                 f"| {', '.join(i for _, i in ids)} | {who} |")
    L.append("")
    for pk, (name, definition, rule) in PATTERNS.items():
        ids = by_pat[pk]
        arguable = [i for _, i in ids if CASE_PATTERN[i][1]]
        L.append(f"### {pk} — {name} ({len(ids)} cases)")
        L.append("")
        L.append(definition)
        L.append("")
        L.append(f"**Rule that should have caught it:** {rule}")
        L.append("")
        L.append("**Which models fall for it:** "
                 + "; ".join(f"{SHORT[m]} {sum(1 for si in ids if si in wrong[m])}/{len(ids)}"
                             for m in MODELS) + ".")
        L.append("")
        if arguable:
            L.append(f"**Arguable truth labels ({len(arguable)}):** " + ", ".join(arguable)
                     + ". These are flagged rather than counted as model errors; see the notes "
                       "on each case in section 1.")
        else:
            L.append("**Arguable truth labels:** none — every case in this cluster is a clear "
                     "negative under the settled rules.")
        L.append("")
        L.append("Cases: " + ", ".join(f"`{i}`" for _, i in ids) + ".")
        L.append("")

    n_arg = sum(1 for _, i in leaked if CASE_PATTERN[i][1])
    L.append(f"### Arguable cases, collected ({n_arg} of {len(leaked)})")
    L.append("")
    L += ["| id | pattern | why the `no` label is contestable |", "| --- | --- | --- |"]
    for s, i in leaked:
        pk, arg, note = CASE_PATTERN[i]
        if arg:
            L.append(f"| {i} | {pk} | {esc(note)} |")
    L.append("")
    L.append("Two of these clusters exist because the prompt contradicts itself rather than "
             "because a model misread the abstract. Qualifying rule 4 accepts \"re-testing, "
             "re-validating or evaluating the reproducibility of an already published "
             "instrument, scale, test or clinical procedure … including when this is done in a "
             "new population, language or setting\"; non-qualifying sense 1 rejects "
             "\"inter-rater agreement, test-retest agreement\". A Persian translation of a "
             "published dizziness inventory whose stated aim is its test-retest reproducibility "
             "(`H003`) satisfies both sentences at once, and the three models split three ways "
             "on it. No amount of confidence calibration resolves that; the rule has to pick a "
             "side, which is what proposal F3-full in section 5 does — and why it is not "
             "recommended as drafted.")
    L.append("")

    # ------------------------------------------------------------ 3. per-model
    L += ["## 3. Per-model error profiles", "",
          "| model | hard negatives not called `none` | of which `unclear` | of which "
          "qualifying | share of the 74 |", "| --- | --- | --- | --- | --- |"]
    for m in MODELS:
        unc = sum(1 for s, i in wrong[m] if data[(m, s)][i]["verdict"] == "unclear")
        yes = sum(1 for s, i in wrong[m] if data[(m, s)][i]["verdict"] == "yes")
        L.append(f"| {SHORT[m]} | {len(wrong[m])} | {unc} | {yes} "
                 f"| {len(wrong[m]) / len(hard_neg):.0%} |")
    L.append("")
    L.append("Confidence carried by those wrong answers — the axis a gate could in principle "
             "use, if the errors were hedged:")
    L.append("")
    L += ["| model | high | medium | low |", "| --- | --- | --- | --- |"]
    for m in MODELS:
        c = Counter(data[(m, s)][i]["conf"] for s, i in wrong[m])
        L.append(f"| {SHORT[m]} | {c.get('high', 0)} | {c.get('medium', 0)} | {c.get('low', 0)} |")
    L.append("")
    L.append("Characteristic clusters — each model's errors distributed over the taxonomy, as "
             "a share of that model's own error count:")
    L.append("")
    L += ["| pattern | " + " | ".join(SHORT[m] for m in MODELS) + " |",
          "| --- | " + " | ".join("---" for _ in MODELS) + " |"]
    for pk in PATTERNS:
        ids = by_pat[pk]
        cells = []
        for m in MODELS:
            n = sum(1 for si in ids if si in wrong[m])
            cells.append(f"{n} ({n / len(wrong[m]):.0%})" if wrong[m] else "0")
        L.append(f"| {pk} {PATTERNS[pk][0][:44]} | " + " | ".join(cells) + " |")
    L.append("")

    L += ["**Overlap matrix.** For each pair, cases both get wrong versus cases only one does:",
          "", "| pair | both wrong | only the first | only the second | union |",
          "| --- | --- | --- | --- | --- |"]
    for a, b in itertools.combinations(MODELS, 2):
        L.append(f"| {SHORT[a]} vs {SHORT[b]} | {len(wrong[a] & wrong[b])} "
                 f"| {len(wrong[a] - wrong[b])} | {len(wrong[b] - wrong[a])} "
                 f"| {len(wrong[a] | wrong[b])} |")
    L.append("")
    all3 = wrong[FL] & wrong[GPT] & wrong[MIN]
    L.append(f"All three models are wrong on {len(all3)} cases "
             f"({', '.join(sorted(i for _, i in all3))}); the union of all three is "
             f"{len(set().union(*wrong.values()))}.")
    L.append("")
    L.append("**No model's errors are a subset of another's.** ministral-14b is the closest "
             f"thing to a superset — it covers {len(wrong[FL] & wrong[MIN])} of flash-lite's "
             f"{len(wrong[FL])} — but it still leaves {len(wrong[FL] - wrong[MIN])} flash-lite "
             f"errors uncovered and adds {len(wrong[MIN] - wrong[FL])} of its own. "
             "gpt-5.4-mini is the near-orthogonal voter: it is wrong on the fewest cases "
             f"({len(wrong[GPT])}) and shares only {len(wrong[GPT] & wrong[MIN])} of them with "
             "ministral-14b, which is why a trio gate outperforms either pair on the discard "
             "axis in `gate_sweep.md`.")
    L.append("")
    L.append("The profiles have distinct shapes:")
    L.append("")
    internal = by_pat["P1"] + by_pat["P2"]
    L.append("- **flash-lite** over-weights the *self-declaration* rule. Whenever the abstract "
             "contains the word 'replication' attached to anything, it tends to answer "
             "qualifying at high confidence and quote that exact phrase — 'a replication of one "
             "another' (`HU05`), 'a replication of Study 2' (`HU34`), 'replication of the top "
             "SNPs' (`HU27`), '16 elements of replication of Ramirez and Gordillo's research' "
             "(`HU44`). Its dominant clusters are the two internal-replication patterns P1 and "
             f"P2, where it is wrong on {sum(1 for si in internal if si in wrong[FL])} of the "
             f"{len(internal)} cases. It is comparatively good at tool benchmarking (P3, "
             f"{sum(1 for si in by_pat['P3'] if si in wrong[FL])} of {len(by_pat['P3'])}), which "
             "it recognises by name.")
    gpt_med = sorted(i for s, i in wrong[GPT] if data[(GPT, s)][i]["conf"] == "medium")
    L.append("- **gpt-5.4-mini** is the strictest on declared intent and the only model that "
             "reliably says 'internal discovery/replication design' in so many words. Its "
             f"errors are the opposite kind: it is the model most willing to *infer* a re-test "
             f"the abstract does not declare, and {len(gpt_med)} of its {len(wrong[GPT])} errors "
             f"are at `medium` confidence ({', '.join('`' + i + '`' for i in gpt_med)}), i.e. it "
             "is signalling the doubt. Under a gate that requires high confidence to discard, a "
             "medium-confidence *qualifying* verdict still blocks the discard just as hard as a "
             "high-confidence one, so this calibration buys the gate nothing as the rule is "
             "currently written.")
    L.append("- **ministral-14b** is the most permissive and the widest-spread: it is wrong on "
             f"{len(wrong[MIN])} of 74 and dominates the tool-benchmark cluster (P3, 8 of 8) "
             "and the incidental-agreement cluster. Its reasoning sentences routinely upgrade "
             "'compared with' or 'verified against' into 're-analyses and reproduces a specific "
             "finding' (`HU53`, `HU59`, `HU60`, `H001`). It also produces the empty "
             "`evidence_quote` on several `none` verdicts, so its quotes cannot be used as a "
             "gate signal.")
    L.append("")

    # ------------------------------------------------------------ 4. proxy pool
    proxy_cases = load_cases("coding_v3_cases.json")
    proxy = read_v3(FL, "coding_v3")
    kept = [i for i in proxy_cases if proxy[i]["verdict"] != "no"]
    buckets = Counter(PROXY_SPLIT[i][0] for i in kept)

    L += ["## 4. Proxy-pool cross-check — the 150 past production discards", "",
          f"`voter_v3_gemini-3.5-flash-lite_coding_v3.json` holds flash-lite's v3 verdicts on "
          f"{len(proxy_cases)} rows the live screen previously discarded as not-a-replication. "
          f"{len(kept)} were not re-discarded. Splitting them by reading each title and "
          "abstract:", ""]
    L += ["| bucket | n | share of the 29 | share of the 150 |", "| --- | --- | --- | --- |"]
    names = {"a": "(a) plausibly genuine replications the old screen wrongly discarded — v3 right",
             "b": "(b) v3 errors matching a taxonomy pattern",
             "c": "(c) unclear"}
    for k in ("a", "b", "c"):
        L.append(f"| {names[k]} | {buckets[k]} | {buckets[k] / len(kept):.0%} "
                 f"| {buckets[k] / len(proxy_cases):.1%} |")
    L.append("")
    for k in ("a", "b", "c"):
        L.append(f"### {names[k]} — {buckets[k]} cases")
        L.append("")
        L += ["| id | verdict | conf | categories | evidence quote | model reasoning | pattern "
              "| my reading |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for i in kept:
            b, pk, note = PROXY_SPLIT[i]
            if b != k:
                continue
            r = proxy[i]
            L.append(f"| {i} | {r['cls']} | {r['conf']} | {', '.join(r['cats'])} "
                     f"| {esc(r['ev'], 120)} | {esc(r['reason'], 170)} | {pk or '—'} "
                     f"| {esc(note)} |")
        L.append("")
        L.append("Titles: " + "; ".join(f"`{i}` {esc(proxy_cases[i]['title'], 90)}"
                                        for i in kept if PROXY_SPLIT[i][0] == k) + ".")
        L.append("")

    err_pat = Counter(PROXY_SPLIT[i][1] for i in kept if PROXY_SPLIT[i][0] == "b")
    L.append("**How much of the proxy-pool leak is actually error.** Of the "
             f"{len(kept)} rows v3 did not re-discard, {buckets['a']} "
             f"({buckets['a'] / len(kept):.0%}) are papers that declare themselves conceptual "
             "replications of a named earlier study and that the old production screen should "
             "not have thrown away — v3 is right and the old screen was wrong. "
             f"{buckets['c']} are genuinely unclear. That leaves {buckets['b']} "
             f"({buckets['b'] / len(kept):.0%} of the 29, {buckets['b'] / len(proxy_cases):.1%} "
             "of the 150) as v3 errors, and every one of them lands in a pattern already named "
             "in section 2: " + ", ".join(f"{pk} ×{n}" for pk, n in err_pat.most_common()) + ".")
    L.append("")
    L.append("The distribution is different from the hard-negative sets: the measurement "
             "clusters P4 and P5 make up "
             f"{(err_pat['P4'] + err_pat['P5']) / max(buckets['b'], 1):.0%} of the proxy-pool "
             "errors against "
             f"{(len(by_pat['P4']) + len(by_pat['P5'])) / len(leaked):.0%} of the hard-negative "
             "leaks — instrument translations and inter-laboratory studies are what the real "
             "production queue is full of, not the adversarially-selected internal-replication "
             "cases. That raises the expected payoff of F3a and lowers the payoff of F4 and F1 "
             "on live traffic relative to what section 5's ranking, computed on the hard "
             "negatives, suggests.")
    L.append("")
    L.append("It also means the leak on the real corpus is mostly, but not entirely, error: "
             f"about {buckets['b'] / len(kept):.0%} of what v3 refuses to re-discard is a "
             "screening mistake, and about "
             f"{buckets['a'] / len(kept):.0%} is v3 correctly rescuing a genuine replication. "
             "Any amendment that removes the whole leak would also remove that "
             f"{buckets['a']}-case rescue, which is the entire reason the prompt was rewritten.")
    L.append("")

    # ------------------------------------------------------------ 5. proposals
    L += ["## 5. Prompt-fix proposals", "",
          "One proposal per taxonomy pattern with ≥2 cases. Nothing here has been applied — "
          "`prompt_v3.txt` is unchanged. Line numbers refer to `prompt_v3.txt` as it stands.",
          "", "### Ranking", "",
          "Ranked by cases addressed per unit of recall risk. \"Cases addressed\" counts the "
          "hard negatives in the pattern, with arguable-label cases shown separately because "
          "fixing those is a labelling decision, not a prompt fix.", "",
          "| rank | id | pattern(s) | cases (of which arguable) | scope | recall risk |",
          "| --- | --- | --- | --- | --- | --- |"]
    for rank, (pid, pks, title, scope, draft, place, risk, level) in enumerate(PROPOSALS, 1):
        n = sum(len(by_pat[pk]) for pk in pks)
        na = sum(1 for pk in pks for _, i in by_pat[pk] if CASE_PATTERN[i][1])
        L.append(f"| {rank} | **{pid}** | {', '.join(pks)} | {n} ({na}) "
                 f"| {scope.split('(')[0].strip()} | {level} |")
    L.append("")
    L.append("F3-full is listed last because its risk is not merely high but pointed at the "
             "exact class of positives the settled coding rules were rewritten to admit; it is "
             "included for completeness, not as a recommendation.")
    L.append("")
    for rank, (pid, pks, title, scope, draft, place, risk, level) in enumerate(PROPOSALS, 1):
        n = sum(len(by_pat[pk]) for pk in pks)
        L.append(f"### {rank}. {pid} — {title}")
        L.append("")
        L.append(f"**Addresses:** {', '.join(pks)} — {n} hard-negative leaks "
                 f"({', '.join(i for pk in pks for _, i in by_pat[pk])}).")
        L.append("")
        L.append(f"**Scope:** {scope}.")
        L.append("")
        L.append("**Per-model exposure in this cluster:** "
                 + "; ".join(f"{SHORT[m]} "
                             f"{sum(1 for pk in pks for si in by_pat[pk] if si in wrong[m])}/{n}"
                             for m in MODELS) + ".")
        L.append("")
        L.append(f"**Where:** {place}")
        L.append("")
        L.append("**Draft:**")
        L.append("")
        L.append(f"> {draft}")
        L.append("")
        L.append(f"**Recall risk ({level}):** {risk}")
        L.append("")

    L.append("### Why almost everything is SHARED")
    L.append("")
    p3_solo = sorted(i for s, i in by_pat["P3"]
                     if (s, i) in wrong[MIN] and (s, i) not in wrong[FL]
                     and (s, i) not in wrong[GPT])
    L.append("A model-specific segment only earns its keep when the pattern is specific to that "
             "model, and the overlap matrix in section 3 says almost none of them are. The one "
             f"pattern that comes close is P3 (tool benchmarking), where ministral-14b is wrong "
             f"on all {len(by_pat['P3'])} cases and the other two on "
             f"{sum(1 for si in by_pat['P3'] if si in wrong[FL])} each — a ministral-only "
             f"segment would fix {len(p3_solo)} leaks the others do not have "
             f"({', '.join('`' + i + '`' for i in p3_solo)}). Even there the shared version is "
             "preferable: F2 is a clarification of a rule the prompt already states, so it "
             "cannot hurt a model that already applies it, and diverging prompts would double "
             "the prompt-version cache keys (`prompt_version()` folds the prompt text into the "
             "key, so a per-model prompt means a per-model cache namespace) and double the "
             "maintenance surface for a rule the whole screen depends on.")
    L.append("")
    L.append("The counter-case would be a segment that is *wrong* for one model — for example, "
             "telling gpt-5.4-mini to weigh the stated objectives more heavily (F5) when it is "
             "already the strictest model on declared intent, which risks pushing it further "
             "toward `none` and eroding the one voter whose qualifying verdicts are worth "
             "trusting. If F5 ships, scoring it per model before adopting it for all three is "
             "the cheap precaution.")
    L.append("")
    L.append("### What no prompt fix reaches")
    L.append("")
    n45 = len(by_pat["P4"]) + len(by_pat["P5"])
    n45a = sum(1 for pk in ("P4", "P5") for _, i in by_pat[pk] if CASE_PATTERN[i][1])
    L.append(f"{n_arg} of the {len(leaked)} leaks are flagged as arguable. All {n45} cases in "
             f"P4+P5 sit on the Rule 4 / sense 1 contradiction, and {n45a} of them are close "
             "enough to the line that the label could defensibly go either way. Those are "
             "decided by "
             "re-labelling, not by wording: until the project settles whether a translated "
             "questionnaire's test-retest study is in scope, the models will keep splitting on "
             "them, and any prompt that makes them all agree will be agreeing with a rule the "
             f"truth set does not yet encode. The cheapest next step is to re-code the {n_arg} "
             "arguable cases against the settled rules in "
             "`screening_prompt_proposal.md` §1–2 and see how many are still negatives; that "
             "changes the size of the leak before a single word of the prompt moves.")
    L.append("")

    (HERE / "leak_analysis.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"wrote leak_analysis.md — {len(leaked)} leaked of {len(hard_neg)} hard negatives; "
          f"per-model wrong: " + ", ".join(f"{SHORT[m]} {len(wrong[m])}" for m in MODELS))


if __name__ == "__main__":
    main()
