#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import signal
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd
import requests

ROOT = Path("/home/test/work/pubmed")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analyze_half_life import analyze_drug_folder
from app import api_download

INPUT_XLSX = ROOT / "INN" / "halflife_fc_master_table_elimination_merged_filled_with_fc_subtype_with_inn_sequences.xlsx"
OUT_DIR = ROOT / "reports" / "drug_expert_full"
OUT_DIR.mkdir(parents=True, exist_ok=True)
STATE_JSON = OUT_DIR / "progress_state.json"

NCBI = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def has_drug_mention(text: str, drug: str) -> bool:
    t = (text or "").lower()
    d = (drug or "").strip().lower()
    if not d:
        return False
    if re.search(rf"(?<![a-z0-9]){re.escape(d)}(?![a-z0-9])", t):
        return True
    return norm(d) in norm(t)


def parse_half_life_from_text(text: str):
    if not text:
        return None
    t = re.sub(r"\s+", " ", text)
    pats = [
        r"(?i)(?:terminal\s+)?(?:elimination\s+)?(?:half[- ]life|t1/2)[^0-9]{0,35}([0-9]+(?:\.[0-9]+)?)\s*(day|days|d|hour|hours|hr|hrs|h|week|weeks|w)",
        r"(?i)([0-9]+(?:\.[0-9]+)?)\s*(day|days|d|hour|hours|hr|hrs|h|week|weeks|w)[^\.]{0,80}(?:half[- ]life|t1/2)",
    ]
    for p in pats:
        m = re.search(p, t)
        if m:
            val = float(m.group(1))
            unit = m.group(2).lower()
            if unit.startswith("d"):
                h = val * 24
            elif unit.startswith("w"):
                h = val * 7 * 24
            else:
                h = val
            return {
                "half_life_value": val,
                "half_life_unit": unit,
                "half_life_hours": round(h, 3),
                "half_life_evidence_snippet": t[max(0, m.start() - 100) : m.end() + 120],
            }
    return None


def pubmed_search(query: str, retmax: int = 8):
    r = requests.get(
        f"{NCBI}/esearch.fcgi",
        params={"db": "pubmed", "term": query, "retmax": retmax, "retmode": "json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("esearchresult", {}).get("idlist", [])


def pubmed_fetch(pmids):
    if not pmids:
        return []
    r = requests.get(
        f"{NCBI}/efetch.fcgi",
        params={"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"},
        timeout=30,
    )
    r.raise_for_status()
    root = ET.fromstring(r.text)
    out = []
    for art in root.findall(".//PubmedArticle"):
        pmid = (art.findtext(".//PMID") or "").strip()
        title = (art.findtext(".//ArticleTitle") or "").strip()
        year = (art.findtext(".//PubDate/Year") or "").strip()
        doi = ""
        for a in art.findall(".//ArticleId"):
            if a.attrib.get("IdType") == "doi":
                doi = (a.text or "").strip()
                break
        abs_text = " ".join((x.text or "") for x in art.findall(".//Abstract/AbstractText"))
        out.append(
            {
                "pmid": pmid,
                "title": title,
                "year": year,
                "doi": doi,
                "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
                "doi_url": f"https://doi.org/{doi}" if doi else "",
                "abstract": re.sub(r"\s+", " ", abs_text),
            }
        )
    return out


def classify_eff_tox(text: str):
    t = (text or "").lower()
    eff = any(k in t for k in ["efficacy", "effective", "response", "improvement", "remission", "outcome"])
    tox = any(k in t for k in ["safety", "adverse", "toxicity", "serious", "infection", "event", "tolerab"])
    return eff, tox


def clinicaltrials(drug: str, max_trials: int = 8):
    r = requests.get(
        "https://clinicaltrials.gov/api/v2/studies",
        params={"query.term": drug, "pageSize": max_trials, "format": "json"},
        timeout=30,
    )
    r.raise_for_status()
    rows = []
    for s in r.json().get("studies", []):
        p = s.get("protocolSection", {})
        ident = p.get("identificationModule", {})
        design = p.get("designModule", {})
        status = p.get("statusModule", {})
        cond = p.get("conditionsModule", {})
        arms = p.get("armsInterventionsModule", {})
        nct = ident.get("nctId", "")
        phases = design.get("phases", [])
        rows.append(
            {
                "nct_number": nct,
                "clinicaltrials_url": f"https://clinicaltrials.gov/study/{nct}" if nct else "",
                "official_title": ident.get("officialTitle", ident.get("briefTitle", "")),
                "phase": ",".join(phases) if isinstance(phases, list) else str(phases or "unknown"),
                "enrollment": (design.get("enrollmentInfo", {}) or {}).get("count", "unknown"),
                "status": status.get("overallStatus", "unknown"),
                "conditions": "; ".join(cond.get("conditions", []) or []),
                "interventions": "; ".join(
                    i.get("name", "") for i in (arms.get("interventions", []) or []) if isinstance(i, dict)
                ),
            }
        )
    return rows


def _fda_half_life(drug: str):
    r = requests.get(
        "https://api.fda.gov/drug/label.json",
        params={"search": f'openfda.generic_name:"{drug}"', "limit": 5},
        timeout=30,
    )
    if r.status_code != 200:
        return None
    for item in r.json().get("results", []):
        blocks = []
        for k in ["pharmacokinetics", "clinical_pharmacology", "description"]:
            v = item.get(k, [])
            if isinstance(v, list):
                blocks.extend(v)
            elif isinstance(v, str):
                blocks.append(v)
        t = " ".join(str(x) for x in blocks)
        p = parse_half_life_from_text(t)
        if p:
            p["source_type"] = "fda_label"
            p["source_url"] = "https://api.fda.gov/drug/label.json"
            return p
    return None


def _dailymed_half_life(drug: str):
    r = requests.get(
        "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json",
        params={"drug_name": drug},
        timeout=30,
    )
    if r.status_code != 200:
        return None
    rows = r.json().get("data", []) or []
    for row in rows[:3]:
        setid = row.get("setid", "")
        if not setid:
            continue
        url = f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={setid}"
        try:
            html = requests.get(url, timeout=30).text
        except Exception:
            continue
        txt = re.sub(r"<[^>]+>", " ", html)
        txt = re.sub(r"\s+", " ", txt)
        p = parse_half_life_from_text(txt)
        if p:
            p["source_type"] = "dailymed_label"
            p["source_url"] = url
            return p
    return None


def _drugbank_half_life(drug: str):
    url = f"https://go.drugbank.com/unearth/q?searcher=drugs&query={requests.utils.quote(drug)}"
    try:
        html = requests.get(url, timeout=30).text
    except Exception:
        return None
    txt = re.sub(r"<[^>]+>", " ", html)
    txt = re.sub(r"\s+", " ", txt)
    p = parse_half_life_from_text(txt)
    if p:
        p["source_type"] = "drugbank_secondary"
        p["source_url"] = url
        return p
    return None


def external_label_fallback(drug: str):
    for fn in (_fda_half_life, _dailymed_half_life, _drugbank_half_life):
        try:
            x = fn(drug)
            if x:
                return x
        except Exception:
            continue
    return None


def process_one(drug: str):
    row = {"drug_name": drug}
    signal.alarm(90)
    dl = api_download(term=drug, max_results=4, category=drug)
    signal.alarm(0)
    row.update(
        {
            "total_candidates": dl.get("total_candidates", 0),
            "downloaded_count": dl.get("downloaded_count", 0),
            "deduped_count": dl.get("deduped_count", 0),
            "failed_count": dl.get("failed_count", 0),
        }
    )

    hl = analyze_drug_folder(ROOT / "downloads" / drug)
    row.update(
        {
            "half_life_status": hl.get("status", ""),
            "half_life_value": hl.get("half_life_value", ""),
            "half_life_unit": hl.get("half_life_unit", ""),
            "half_life_hours": hl.get("half_life_hours", ""),
            "half_life_source_file": hl.get("source_file", ""),
            "half_life_evidence_snippet": hl.get("evidence", ""),
            "half_life_source_type": "downloaded_pdf" if hl.get("status") == "found" else "",
            "half_life_nct_number": "not_trial_source",
            "half_life_original_citation": "",
        }
    )

    fallback_links = []
    if hl.get("status") != "found":
        ids = pubmed_search(f"\"{drug}\"[Title/Abstract] AND (half-life OR pharmacokinetics OR t1/2)")
        arts = pubmed_fetch(ids)
        found = None
        for a in arts:
            ta = a.get("title", "") + ". " + a.get("abstract", "")
            if not has_drug_mention(ta, drug):
                continue
            p = parse_half_life_from_text(ta)
            if p:
                found = (a, p)
                break
        if found:
            a, p = found
            row.update(
                {
                    "half_life_status": "found_external_pubmed",
                    "half_life_value": p["half_life_value"],
                    "half_life_unit": p["half_life_unit"],
                    "half_life_hours": p["half_life_hours"],
                    "half_life_evidence_snippet": p["half_life_evidence_snippet"],
                    "half_life_source_type": "external_pubmed_abstract",
                    "half_life_original_citation": a["pubmed_url"] or a["doi_url"],
                }
            )
        else:
            lbl = external_label_fallback(drug)
            if lbl:
                row.update(
                    {
                        "half_life_status": "found_external_label",
                        "half_life_value": lbl["half_life_value"],
                        "half_life_unit": lbl["half_life_unit"],
                        "half_life_hours": lbl["half_life_hours"],
                        "half_life_evidence_snippet": lbl["half_life_evidence_snippet"],
                        "half_life_source_type": lbl.get("source_type", "label_or_database"),
                        "half_life_original_citation": lbl.get("source_url", ""),
                    }
                )
        fallback_links = [
            {
                "drug_name": drug,
                "query_type": "google_scholar",
                "url": f"https://scholar.google.com/scholar?q={requests.utils.quote(drug + ' half-life pharmacokinetics')}",
            },
            {
                "drug_name": drug,
                "query_type": "google_web",
                "url": f"https://www.google.com/search?q={requests.utils.quote(drug + ' half-life pharmacokinetics')}",
            },
            {
                "drug_name": drug,
                "query_type": "pubmed",
                "url": f"https://pubmed.ncbi.nlm.nih.gov/?term={requests.utils.quote(drug + ' half-life pharmacokinetics')}",
            },
            {
                "drug_name": drug,
                "query_type": "fda_label",
                "url": f"https://api.fda.gov/drug/label.json?search=openfda.generic_name:%22{requests.utils.quote(drug)}%22&limit=5",
            },
            {
                "drug_name": drug,
                "query_type": "dailymed",
                "url": f"https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json?drug_name={requests.utils.quote(drug)}",
            },
            {
                "drug_name": drug,
                "query_type": "drugbank",
                "url": f"https://go.drugbank.com/unearth/q?searcher=drugs&query={requests.utils.quote(drug)}",
            },
        ]

    signal.alarm(60)
    ct = clinicaltrials(drug, max_trials=8)
    signal.alarm(0)
    row["clinicaltrials_count"] = len(ct)

    ct_rows = [{**c, "drug_name": drug} for c in ct]
    nct_rows = []
    for c in ct[:6]:
        nct = c["nct_number"]
        if not nct:
            continue
        ids = pubmed_search(f"{nct} {drug}")
        arts = pubmed_fetch(ids[:8])
        eff_n = 0
        tox_n = 0
        for a in arts:
            eff, tox = classify_eff_tox(a["title"] + " " + a["abstract"])
            if eff:
                eff_n += 1
                nct_rows.append(
                    {
                        "drug_name": drug,
                        "nct_number": nct,
                        "phase": c["phase"],
                        "enrollment": c["enrollment"],
                        "evidence_type": "efficacy",
                        "pmid": a["pmid"],
                        "pubmed_url": a["pubmed_url"],
                        "doi": a["doi"],
                        "doi_url": a["doi_url"],
                        "title": a["title"],
                        "year": a["year"],
                        "snippet": a["abstract"][:320],
                    }
                )
            if tox:
                tox_n += 1
                nct_rows.append(
                    {
                        "drug_name": drug,
                        "nct_number": nct,
                        "phase": c["phase"],
                        "enrollment": c["enrollment"],
                        "evidence_type": "toxicity",
                        "pmid": a["pmid"],
                        "pubmed_url": a["pubmed_url"],
                        "doi": a["doi"],
                        "doi_url": a["doi_url"],
                        "title": a["title"],
                        "year": a["year"],
                        "snippet": a["abstract"][:320],
                    }
                )
        nct_rows.append(
            {
                "drug_name": drug,
                "nct_number": nct,
                "phase": c["phase"],
                "enrollment": c["enrollment"],
                "evidence_type": "summary",
                "pmid": "",
                "pubmed_url": "",
                "doi": "",
                "doi_url": "",
                "title": "",
                "year": "",
                "snippet": f"pubmed_hits={len(arts)} efficacy_hits={eff_n} toxicity_hits={tox_n}",
            }
        )
    return row, ct_rows, nct_rows, fallback_links


def load_state():
    if STATE_JSON.exists():
        try:
            return json.loads(STATE_JSON.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state):
    STATE_JSON.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError("timeout")))
    src = pd.read_excel(INPUT_XLSX, sheet_name="Sheet1")
    drugs = sorted(set(src["Primary_name_clean"].dropna().astype(str).str.strip()))

    state = load_state()
    done = set(state.get("done", []))

    summary_rows = state.get("summary_rows", [])
    ct_all = state.get("ct_all", [])
    nct_all = state.get("nct_all", [])
    fallback_all = state.get("fallback_all", [])

    total = len(drugs)
    for i, drug in enumerate(drugs, 1):
        if drug in done:
            continue
        print(f"[{i}/{total}] {drug}")
        try:
            row, ct_rows, nct_rows, fb_rows = process_one(drug)
            summary_rows.append(row)
            ct_all.extend(ct_rows)
            nct_all.extend(nct_rows)
            fallback_all.extend(fb_rows)
        except Exception as e:
            summary_rows.append({"drug_name": drug, "half_life_status": "failed", "error": str(e)})
        done.add(drug)
        state = {
            "done": sorted(done),
            "summary_rows": summary_rows,
            "ct_all": ct_all,
            "nct_all": nct_all,
            "fallback_all": fallback_all,
        }
        save_state(state)
        pd.DataFrame(summary_rows).to_csv(OUT_DIR / "all_drugs_summary_partial.csv", index=False)

    df_summary = pd.DataFrame(summary_rows)
    df_ct = pd.DataFrame(ct_all)
    df_nct = pd.DataFrame(nct_all)
    df_fb = pd.DataFrame(fallback_all)

    final = src.merge(df_summary, left_on="Primary_name_clean", right_on="drug_name", how="left")
    final.to_csv(OUT_DIR / "all_drugs_full_summary.csv", index=False)
    df_ct.to_csv(OUT_DIR / "all_drugs_clinicaltrials.csv", index=False)
    df_nct.to_csv(OUT_DIR / "all_drugs_nct_evidence.csv", index=False)
    df_fb.to_csv(OUT_DIR / "all_drugs_fallback_links.csv", index=False)

    with pd.ExcelWriter(OUT_DIR / "all_drugs_full_report.xlsx", engine="openpyxl") as w:
        final.to_excel(w, sheet_name="summary_full", index=False)
        df_ct.to_excel(w, sheet_name="clinicaltrials", index=False)
        df_nct.to_excel(w, sheet_name="nct_evidence", index=False)
        df_fb.to_excel(w, sheet_name="fallback_links", index=False)

    status_counts = final["half_life_status"].value_counts(dropna=False).to_dict()
    report = OUT_DIR / "all_drugs_report.md"
    lines = [
        "# Drug Expert Full Run Report",
        "",
        f"- Input: `{INPUT_XLSX}`",
        f"- Total drugs: **{len(drugs)}**",
        f"- Status counts: `{status_counts}`",
        f"- ClinicalTrials rows: **{len(df_ct)}**",
        f"- NCT evidence rows: **{len(df_nct)}**",
        "",
        "## Output files",
        "- `all_drugs_full_report.xlsx`",
        "- `all_drugs_full_summary.csv`",
        "- `all_drugs_clinicaltrials.csv`",
        "- `all_drugs_nct_evidence.csv`",
        "- `all_drugs_fallback_links.csv`",
    ]
    report.write_text("\n".join(lines), encoding="utf-8")
    print("Done:", OUT_DIR)


if __name__ == "__main__":
    main()

