---
name: drug-expert
description: End-to-end drug evidence workflow: retrieve literature, download OA PDFs, extract PK (including half-life), fetch ClinicalTrials.gov NCT/phase/enrollment, and link NCT to PubMed/Google Scholar efficacy-toxicity evidence with explicit provenance.
---

# OpenClaw Drug Literature

## Purpose

Given a drug name, complete this workflow end-to-end:
1. Retrieve related literature.
2. Return clickable DOI/PubMed/download links.
3. Download open-access PDFs to local folder.
4. Extract PK evidence from downloaded PDFs (half-life, clearance, Vd, Cmax, AUC, Tmax when available).
5. Fetch ClinicalTrials.gov records and summarize NCT number, phase, enrollment/sample size, and status.
6. For each key NCT, find linked efficacy and toxicity evidence in PubMed and Google Scholar.
7. Build explicit provenance: half-life value -> source type -> linked NCT (if any) -> original citation.
8. If PubMed/OA evidence is insufficient, provide paywalled and non-PubMed external links for manual download and extraction.

## Inputs

- `drug_name` (required): e.g., `reslizumab`
- `max_results` (optional, default `12`, range `1-200`)
- `category` (optional, default equals `drug_name`)
- `max_trials` (optional, default `20`): max ClinicalTrials records to summarize
- `need_pk_fields` (optional, default `true`): whether to extract PK fields beyond half-life

## Decision Flow (Must Follow)

1. Run OA download + local PDF extraction first.
2. Extract half-life and other PK fields from downloaded PDFs.
3. Query ClinicalTrials.gov and collect NCT metadata:
   - NCT number
   - phase
   - enrollment/sample size
   - condition
   - intervention
   - status
4. For each top NCT, search:
   - PubMed with `NCTxxxxxxx` and drug aliases
   - Google Scholar with `NCTxxxxxxx <drug_name> efficacy toxicity`
5. Build NCT-linked evidence table (efficacy and toxicity).
6. Resolve half-life provenance:
   - If from trial text/publication tied to NCT, report NCT.
   - If from non-trial review/label/database, mark as `non_trial_source` and give original citation.
7. If half-life is still not found:
   - Query regulatory/database PK sources in this order:
     - FDA label (openFDA drug label)
     - DailyMed SPL label
     - DrugBank summary page
   - If explicit half-life is found there, report as `found_external_label` with source URL.
8. If still not found:
   - Return manual non-OA/paywalled links from query results.
   - Run external search and extract explicit PK statements when possible.
9. Return merged results with priority ranking:
   - Tier A: explicit PK values (half-life preferred) with evidence + provenance
   - Tier B: NCT-linked efficacy/toxicity evidence without explicit PK
   - Tier C: background links only

## Required Commands

Run from project root.

### 1) Download OA PDFs

```bash
python3 - <<'PY'
from app import api_download
import json

drug_name = "reslizumab"
max_results = 12
category = "reslizumab"

r = api_download(term=drug_name, max_results=max_results, category=category)
print(json.dumps(r, ensure_ascii=False, indent=2))
PY
```

### 2) Extract Half-Life From Downloaded PDFs

```bash
python3 - <<'PY'
from pathlib import Path
import pandas as pd
from analyze_half_life import analyze_drug_folder

category = "reslizumab"
folder = Path("/home/test/work/pubmed/downloads") / category
result = analyze_drug_folder(folder)
print(result)

out = Path("/home/test/work/pubmed/reports")
out.mkdir(parents=True, exist_ok=True)
pd.DataFrame([result]).to_excel(out / f"{category}_half_life_report.xlsx", index=False)
PY
```

### 3) Extract Additional PK Fields From Downloaded Text (Optional but recommended)

```bash
python3 - <<'PY'
import re
from pathlib import Path
from pypdf import PdfReader
import pandas as pd

category = "reslizumab"
folder = Path("/home/test/work/pubmed/downloads") / category
patterns = {
    "half_life": r"(?i)(?:half[- ]life|t1/2)[^\\n]{0,80}",
    "clearance": r"(?i)\\b(?:clearance|CL)\\b[^\\n]{0,80}",
    "volume_of_distribution": r"(?i)(?:volume of distribution|\\bVd\\b)[^\\n]{0,80}",
    "cmax": r"(?i)\\bCmax\\b[^\\n]{0,80}",
    "auc": r"(?i)\\bAUC\\b[^\\n]{0,80}",
    "tmax": r"(?i)\\bTmax\\b[^\\n]{0,80}",
}
rows = []
for pdf in sorted(folder.glob("*.pdf")):
    try:
        reader = PdfReader(str(pdf))
        text = " ".join((p.extract_text() or "") for p in reader.pages[:6])
    except Exception:
        continue
    text = re.sub(r"\\s+", " ", text)
    for k, pat in patterns.items():
        m = re.search(pat, text)
        if m:
            rows.append({"file": pdf.name, "field": k, "evidence": m.group(0)[:400]})

out = Path("/home/test/work/pubmed/reports")
out.mkdir(parents=True, exist_ok=True)
pd.DataFrame(rows).to_excel(out / f"{category}_pk_fields_report.xlsx", index=False)
print("rows", len(rows))
PY
```

### 4) Fetch ClinicalTrials.gov NCT, Phase, Enrollment

```bash
python3 - <<'PY'
import requests
import pandas as pd
from pathlib import Path

drug_name = "reslizumab"
url = "https://clinicaltrials.gov/api/v2/studies"
params = {
    "query.term": drug_name,
    "pageSize": 20,
    "format": "json",
}
r = requests.get(url, params=params, timeout=30)
r.raise_for_status()
data = r.json()

rows = []
for s in data.get("studies", []):
    proto = s.get("protocolSection", {})
    ident = proto.get("identificationModule", {})
    design = proto.get("designModule", {})
    status = proto.get("statusModule", {})
    cond = proto.get("conditionsModule", {})
    arms = proto.get("armsInterventionsModule", {})
    rows.append({
        "nct_number": ident.get("nctId", ""),
        "official_title": ident.get("officialTitle", ident.get("briefTitle", "")),
        "phase": ",".join(design.get("phases", [])) if isinstance(design.get("phases", []), list) else str(design.get("phases", "")),
        "enrollment": (design.get("enrollmentInfo", {}) or {}).get("count", ""),
        "status": status.get("overallStatus", ""),
        "conditions": "; ".join(cond.get("conditions", []) or []),
        "interventions": "; ".join(i.get("name","") for i in (arms.get("interventions", []) or []) if isinstance(i, dict)),
    })

out = Path("/home/test/work/pubmed/reports")
out.mkdir(parents=True, exist_ok=True)
pd.DataFrame(rows).to_excel(out / f"{drug_name}_clinicaltrials_nct_summary.xlsx", index=False)
print("nct_rows", len(rows))
PY
```

### 5) For Each NCT, Search PubMed and Google Scholar For Efficacy/Toxicity

Use both query families:

- PubMed:
  - `NCTxxxxxxx <drug_name> efficacy`
  - `NCTxxxxxxx <drug_name> safety`
  - `NCTxxxxxxx <drug_name> adverse events`
- Google Scholar:
  - `NCTxxxxxxx <drug_name> efficacy toxicity`
  - `NCTxxxxxxx randomized trial`
  - `NCTxxxxxxx phase <phase>`

Collect at least one efficacy and one toxicity evidence item when available for each key NCT.

### 6) If No Half-Life Found: Build Manual Non-OA Link List

Use the download API response fields (`failed`, `skipped`, `deduped`) and convert to clickable links:

- DOI: `https://doi.org/<doi>`
- PubMed: `https://pubmed.ncbi.nlm.nih.gov/<pmid>/`
- Keep reason field (`No OA PDF URL found`, `No PMCID`, etc.)

### 7) If Still Missing: Search External Sources

Search with multiple query variants:
- `<drug_name> half-life`
- `<drug_name> pharmacokinetics`
- `<drug_name> t1/2`
- `<drug_name> phase I pharmacokinetics`
- known aliases/code names + above terms

Preferred external source types:
- journal full-text pages
- conference abstract pages
- trial registry result pages
- company medical/publication pages
- FDA/openFDA label pages
- DailyMed SPL label pages
- DrugBank summary pages

## External Extraction Rule

From external content, only report half-life as `found` if:
- a numeric value is present, and
- unit is present (`hour/day/week`), and
- value is tied to the drug context.

Otherwise classify as:
- `external_pk_link_only` (PK likely but no explicit half-life in page text)

## Output Requirements

Always report:
- Download summary: `downloaded_count`, `deduped_count`, `skipped_count`, `failed_count`
- File location: `downloads/<category>/`
- Literature links:
  - DOI: `https://doi.org/<doi>` when DOI exists
  - PubMed: `https://pubmed.ncbi.nlm.nih.gov/<pmid>/` when PMID exists
- PK extraction summary:
  - `half_life_value`, `half_life_unit`, `half_life_hours`
  - optional `clearance`, `volume_of_distribution`, `cmax`, `auc`, `tmax` (with units if present)
  - evidence sentence snippet and source file for each field
- ClinicalTrials summary:
  - `nct_number`, `phase`, `enrollment`, `status`, `condition`, `intervention`
- NCT-linked evidence:
  - `nct_number`
  - `efficacy_evidence` (citation + link + short finding)
  - `toxicity_evidence` (citation + link + short finding)
- Manual non-OA section (if applicable):
  - paywalled/non-OA links with reason and priority
- External links section (if applicable):
  - non-PubMed links with source type and extraction status
- Half-life provenance chain (mandatory):
  - `half_life_status` (`found` or `not_found_in_downloaded_pdfs`)
  - `half_life_source_type` (`trial_publication` | `review` | `label` | `database` | `external_web`)
  - `half_life_nct_number` (if trial-linked; otherwise `not_trial_source`)
  - `half_life_original_citation` (PMID/DOI/URL)
  - `half_life_source_file` (if local PDF-based)
  - `half_life_evidence_snippet`

## Link Construction Rules

- DOI link: `https://doi.org/<doi>`
- PubMed link: `https://pubmed.ncbi.nlm.nih.gov/<pmid>/`
- Local file path from API field `file_path`
- ClinicalTrials link: `https://clinicaltrials.gov/study/<NCTID>`

## If No Half-Life Is Found

Return:
- `status: not_found_in_downloaded_pdfs`
- a manual non-OA link list first (DOI/PubMed)
- a non-PubMed external link list second
- recommendation to download highest-priority links and rerun extraction

## Quality Checks

- Do not claim full-text download when file header is not PDF.
- Prefer OA download paths returned by API.
- Keep evidence tied to one concrete source file.
- Mark each source as OA/local, non-OA/manual, or external/web.
- Do not claim NCT linkage unless either:
  - the publication explicitly includes that NCT number, or
  - a direct registry-publication link can be verified.
- If phase or enrollment is missing in registry output, return explicit `unknown` (do not guess).
- Clearly separate `efficacy` vs `toxicity/safety` findings in the final summary.

## Other Skills

This workflow does **not** invoke other Cursor Skills. It uses Python modules in this folder (`app.py`, `analyze_half_life.py`, etc.). For batch runs and PDF false-positive checks, see `ARCHITECTURE.md` and `scripts/run_drug_expert_full_from_inn_table.py`, `scripts/generate_per_drug_dossiers.py` in the pubmed repo (also mirrored on GitHub).

## Batch / dossier (recommended for reporting)

After per-drug PDF extraction, run dossier generation with **alias validation** before trusting `half_life_hours`:

- Mark `FALSE_POSITIVE` when the evidence sentence does not mention the drug or its aliases.
- Prefer `VALIDATED` local PDF rows, then DrugBank/DailyMed text with parsed units.
- Merge into master table via `build_dossier_master_summary.py` (writes a **new** xlsx; does not overwrite the FC master file).
