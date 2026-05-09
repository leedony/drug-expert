import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests


INPUT_XLSX = Path("/home/test/work/pubmed/reports/halflife_integrated_master_enhanced_merged_day.xlsx")
OUTPUT_XLSX = Path("/home/test/work/pubmed/reports/halflife_integrated_master_gemini_filled.xlsx")
STATE_JSON = Path("/home/test/work/pubmed/reports/halflife_gemini_state.json")

MODEL = "gemini-2.5-flash"
API_BASE = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"


def clean(v: Any) -> str:
    if pd.isna(v):
        return ""
    s = str(v).strip()
    return "" if s.lower() in {"nan", "none", "nat"} else s


def to_float(v: Any) -> Optional[float]:
    s = clean(v)
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)", s)
        return float(m.group(1)) if m else None


def convert_to_day(value: Optional[float], unit: str) -> Optional[float]:
    if value is None:
        return None
    u = clean(unit).lower()
    if u in {"day", "days", "d"}:
        return value
    if u in {"hour", "hours", "h", "hr", "hrs"}:
        return value / 24.0
    if u in {"week", "weeks", "w"}:
        return value * 7.0
    return None


def parse_half_life_from_text(text: str) -> Tuple[Optional[float], Optional[str], str]:
    t = clean(text)
    if not t:
        return None, None, ""
    patterns = [
        r"(?i)(?:mean\s+)?(?:terminal\s+)?(?:elimination\s+)?half[-\s]?life[^0-9]{0,30}([0-9]+(?:\.[0-9]+)?)\s*(day|days|d|hour|hours|h|week|weeks|w)",
        r"(?i)t1/2[^0-9]{0,20}([0-9]+(?:\.[0-9]+)?)\s*(day|days|d|hour|hours|h|week|weeks|w)",
    ]
    for p in patterns:
        m = re.search(p, t)
        if m:
            value = float(m.group(1))
            unit = m.group(2).lower()
            # evidence sentence
            sentence = ""
            parts = re.split(r"(?<=[.!?])\s+", t)
            for s in parts:
                if re.search(re.escape(m.group(0)), s, flags=re.I):
                    sentence = s.strip()
                    break
            if not sentence:
                sentence = t[:400]
            return value, unit, sentence
    return None, None, ""


def normalize_unit(unit: Optional[str]) -> str:
    u = clean(unit).lower()
    if u in {"day", "days", "d"}:
        return "day"
    if u in {"hour", "hours", "h", "hr", "hrs"}:
        return "hour"
    if u in {"week", "weeks", "w"}:
        return "week"
    return ""


def ask_gemini(drug_name: str, api_key: str, retries: int = 2) -> Dict[str, Any]:
    prompt = f"""
Find the elimination half-life for the drug: {drug_name}.
Search web and answer with concise factual text.
Requirements:
1) Give explicit numeric half-life and unit if found.
2) Include one evidence sentence.
3) Include source links.
If not found, explicitly say not found.
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 600},
    }
    url = f"{API_BASE}?key={api_key}"
    last_err = ""
    for i in range(retries):
        try:
            resp = requests.post(url, json=payload, timeout=35)
            resp.raise_for_status()
            data = resp.json()
            txt = ""
            urls: List[str] = []
            for cand in data.get("candidates", []):
                for part in cand.get("content", {}).get("parts", []):
                    if "text" in part:
                        txt += part["text"] + "\n"
                gm = cand.get("groundingMetadata", {})
                for ch in gm.get("groundingChunks", []):
                    web = ch.get("web", {})
                    uri = clean(web.get("uri", ""))
                    if uri:
                        urls.append(uri)
            value, unit, evidence = parse_half_life_from_text(txt)
            unit = normalize_unit(unit)
            found = value is not None and bool(unit)
            source_url = urls[0] if urls else ""
            return {
                "found": found,
                "half_life_value": value,
                "half_life_unit": unit,
                "source_url": source_url,
                "evidence": evidence or txt[:400],
                "confidence": "medium" if found else "low",
                "notes": "" if found else "not_found_from_model_text",
                "raw_text": txt[:1200],
            }
        except Exception as e:
            last_err = str(e)
            time.sleep(1.0 * (i + 1))
    return {
        "found": False,
        "half_life_value": None,
        "half_life_unit": None,
        "source_url": "",
        "evidence": "",
        "confidence": "low",
        "notes": f"gemini_error: {last_err}",
    }


def load_state() -> Dict[str, Dict[str, Any]]:
    if STATE_JSON.exists():
        try:
            return json.loads(STATE_JSON.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: Dict[str, Dict[str, Any]]) -> None:
    STATE_JSON.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Fill half-life using Gemini")
    parser.add_argument("--all-drugs", action="store_true", help="Query Gemini for all drugs, not only missing rows")
    args = parser.parse_args()

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")

    df = pd.read_excel(INPUT_XLSX, sheet_name="integrated_merged_day")
    if "Primary_name_clean" not in df.columns:
        raise ValueError("Primary_name_clean missing in input")
    if "final_half_life_value" not in df.columns:
        raise ValueError("final_half_life_value missing in input")

    state = load_state()

    # Process scope
    missing_mask = df["final_half_life_value"].apply(lambda x: to_float(x) is None)
    if args.all_drugs:
        missing = df.copy()
    else:
        missing = df[missing_mask].copy()
    total = len(missing)

    pending: list[str] = []
    for row in missing.itertuples(index=False):
        drug = clean(getattr(row, "Primary_name_clean", ""))
        if not drug:
            continue
        if drug in state and state[drug].get("done"):
            continue
        pending.append(drug)

    done_count = len(state)
    if pending:
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(ask_gemini, d, api_key): d for d in pending}
            for i, fut in enumerate(as_completed(futs), start=1):
                drug = futs[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    res = {
                        "found": False,
                        "half_life_value": None,
                        "half_life_unit": None,
                        "source_url": "",
                        "evidence": "",
                        "confidence": "low",
                        "notes": f"future_error: {e}",
                    }
                val = to_float(res.get("half_life_value"))
                unit = clean(res.get("half_life_unit"))
                day_val = convert_to_day(val, unit)
                state[drug] = {
                    "done": True,
                    "found": bool(res.get("found", False)),
                    "half_life_value": val,
                    "half_life_unit": unit,
                    "half_life_value_day": day_val,
                    "source_url": clean(res.get("source_url", "")),
                    "evidence": clean(res.get("evidence", "")),
                    "confidence": clean(res.get("confidence", "")),
                    "notes": clean(res.get("notes", "")),
                }
                if i % 20 == 0 or i == len(pending):
                    print(
                        f"[{done_count + i}/{total}] {drug}: found={state[drug]['found']} day={state[drug]['half_life_value_day']}",
                        flush=True,
                    )
                    save_state(state)
        save_state(state)

    # Merge back
    df["drug_key"] = df["Primary_name_clean"].apply(lambda x: clean(x))
    gdf = pd.DataFrame(
        [
            {
                "drug_key": k,
                "gemini_found": v.get("found", False),
                "gemini_half_life_value": v.get("half_life_value"),
                "gemini_half_life_unit": v.get("half_life_unit"),
                "gemini_half_life_value_day": v.get("half_life_value_day"),
                "gemini_source_url": v.get("source_url", ""),
                "gemini_evidence": v.get("evidence", ""),
                "gemini_confidence": v.get("confidence", ""),
                "gemini_notes": v.get("notes", ""),
            }
            for k, v in state.items()
        ]
    )
    out = df.merge(gdf, on="drug_key", how="left")

    # Fill final half-life only when missing
    def fill_final(row):
        final_v = to_float(row.get("final_half_life_value"))
        if final_v is not None:
            return row.get("final_half_life_value"), row.get("final_half_life_unit"), row.get("final_half_life_source"), row.get("final_confidence_note")
        gv = to_float(row.get("gemini_half_life_value_day"))
        if gv is not None:
            return gv, "day", "gemini_web_search", clean(row.get("gemini_confidence", ""))
        return row.get("final_half_life_value"), row.get("final_half_life_unit"), row.get("final_half_life_source"), row.get("final_confidence_note")

    filled = out.apply(fill_final, axis=1, result_type="expand")
    out["final_half_life_value"] = filled[0]
    out["final_half_life_unit"] = filled[1]
    out["final_half_life_source"] = filled[2]
    out["final_confidence_note"] = filled[3]

    with pd.ExcelWriter(OUTPUT_XLSX, engine="openpyxl") as w:
        summary = pd.DataFrame(
            [
                {
                    "input_rows": len(df),
                    "missing_before": int(missing_mask.sum()),
                    "all_drugs_mode": bool(args.all_drugs),
                    "gemini_found_count": int(out["gemini_found"].fillna(False).sum()),
                    "final_filled_after": int(out["final_half_life_value"].apply(lambda x: to_float(x) is not None).sum()),
                }
            ]
        )
        summary.to_excel(w, sheet_name="summary", index=False)
        out.drop(columns=["drug_key"]).to_excel(w, sheet_name="integrated_with_gemini", index=False)
        out[out["gemini_found"].fillna(False)].drop(columns=["drug_key"]).to_excel(w, sheet_name="gemini_found_only", index=False)

    print(f"written {OUTPUT_XLSX}", flush=True)


if __name__ == "__main__":
    main()
