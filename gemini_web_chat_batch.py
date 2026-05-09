import argparse
import re
import time
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DEFAULT_PROMPT = (
    "Please provide the elimination half-life of {drug}. "
    "Return: 1) numeric half-life with unit, 2) one evidence sentence, 3) source links."
)


def clean(v) -> str:
    if pd.isna(v):
        return ""
    s = str(v).strip()
    return "" if s.lower() in {"nan", "none", "nat"} else s


def extract_half_life(text: str) -> Tuple[Optional[float], Optional[str]]:
    if not text:
        return None, None
    patterns = [
        r"(?i)(?:mean\s+)?(?:terminal\s+)?(?:elimination\s+)?half[-\s]?life[^0-9]{0,30}([0-9]+(?:\.[0-9]+)?)\s*(day|days|d|hour|hours|h|week|weeks|w)",
        r"(?i)t1/2[^0-9]{0,20}([0-9]+(?:\.[0-9]+)?)\s*(day|days|d|hour|hours|h|week|weeks|w)",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return float(m.group(1)), m.group(2).lower()
    return None, None


def to_day(value: Optional[float], unit: Optional[str]) -> Optional[float]:
    if value is None or not unit:
        return None
    u = unit.lower()
    if u in {"day", "days", "d"}:
        return value
    if u in {"hour", "hours", "h"}:
        return value / 24.0
    if u in {"week", "weeks", "w"}:
        return value * 7.0
    return None


def read_drug_names(xlsx: Path, column: str, sheet: Optional[str] = None) -> List[str]:
    if sheet:
        df = pd.read_excel(xlsx, sheet_name=sheet)
    else:
        xl = pd.ExcelFile(xlsx)
        df = None
        for s in xl.sheet_names:
            tmp = pd.read_excel(xlsx, sheet_name=s, nrows=5)
            if column in tmp.columns:
                df = pd.read_excel(xlsx, sheet_name=s)
                break
        if df is None:
            raise ValueError(f"Column not found in any sheet: {column}")
    if column not in df.columns:
        raise ValueError(f"Column not found: {column}")
    names = [clean(x) for x in df[column].tolist()]
    names = [x for x in names if x]
    # de-duplicate preserve order
    seen = set()
    out = []
    for n in names:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            out.append(n)
    return out


def get_input_locator(page):
    selectors = [
        "textarea",
        "div[contenteditable='true'][role='textbox']",
        "div[contenteditable='true']",
    ]
    for sel in selectors:
        loc = page.locator(sel)
        try:
            if loc.count() > 0 and loc.first.is_visible():
                return loc.first
        except Exception:
            continue
    return None


def collect_best_response_text(page) -> str:
    # Gemini DOM 变化比较频繁，采用多选择器兜底，返回最长文本块
    selectors = [
        "[data-message-author-role='model']",
        "model-response",
        "article",
        "main div",
    ]
    best = ""
    for sel in selectors:
        try:
            loc = page.locator(sel)
            n = min(loc.count(), 20)
            for i in range(n):
                t = (loc.nth(i).inner_text(timeout=500) or "").strip()
                if len(t) > len(best):
                    best = t
        except Exception:
            continue
    return best


def ask_one_drug(page, drug: str, prompt_template: str, wait_seconds: int = 45) -> str:
    prompt = prompt_template.format(drug=drug)
    input_box = get_input_locator(page)
    if input_box is None:
        raise RuntimeError("Cannot find Gemini input box. Please refresh page and retry.")

    # Send prompt
    tag = input_box.evaluate("el => el.tagName.toLowerCase()")
    if tag == "textarea":
        input_box.fill(prompt)
    else:
        input_box.click()
        input_box.press("Control+A")
        input_box.type(prompt, delay=5)
    page.keyboard.press("Enter")

    # Wait and poll for response
    start = time.time()
    best = ""
    while time.time() - start < wait_seconds:
        time.sleep(2)
        txt = collect_best_response_text(page)
        if len(txt) > len(best):
            best = txt
    return best


def main():
    parser = argparse.ArgumentParser(description="Batch chat with Gemini web UI")
    parser.add_argument(
        "--input-xlsx",
        default="/home/test/work/pubmed/reports/halflife_integrated_master_gemini_filled_clean.xlsx",
        help="Input xlsx path",
    )
    parser.add_argument(
        "--input-column",
        default="Primary_name_clean",
        help="Column name with drug names",
    )
    parser.add_argument("--input-sheet", default="", help="Optional sheet name")
    parser.add_argument(
        "--output-xlsx",
        default="/home/test/work/pubmed/reports/gemini_web_batch_results.xlsx",
        help="Output xlsx path",
    )
    parser.add_argument(
        "--user-data-dir",
        default="/home/test/work/pubmed/.playwright-gemini-profile",
        help="Persistent browser profile directory",
    )
    parser.add_argument("--max-drugs", type=int, default=0, help="Limit count for testing")
    parser.add_argument("--prompt-template", default=DEFAULT_PROMPT, help="Prompt template, use {drug}")
    parser.add_argument("--auto-start", action="store_true", help="Do not wait for terminal input; start automatically")
    parser.add_argument("--startup-wait-seconds", type=int, default=8, help="Seconds to wait before starting in auto mode")
    args = parser.parse_args()

    in_xlsx = Path(args.input_xlsx)
    out_xlsx = Path(args.output_xlsx)
    out_xlsx.parent.mkdir(parents=True, exist_ok=True)

    drugs = read_drug_names(in_xlsx, args.input_column, sheet=args.input_sheet or None)
    if args.max_drugs and args.max_drugs > 0:
        drugs = drugs[: args.max_drugs]

    rows = []
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=args.user_data_dir,
            headless=False,
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        page.goto("https://gemini.google.com/app", wait_until="domcontentloaded")
        print("Please login Gemini in the opened browser if needed.")
        if args.auto_start:
            print(f"Auto-start enabled. Waiting {args.startup_wait_seconds}s before batch...")
            time.sleep(max(args.startup_wait_seconds, 0))
        else:
            input("After login and page ready, press Enter here to start batch... ")

        for i, drug in enumerate(drugs, start=1):
            try:
                response = ask_one_drug(page, drug=drug, prompt_template=args.prompt_template)
                value, unit = extract_half_life(response)
                day_val = to_day(value, unit)
                rows.append(
                    {
                        "drug_name": drug,
                        "found": value is not None,
                        "half_life_value": value,
                        "half_life_unit": unit or "",
                        "half_life_day": day_val,
                        "response_text": response[:4000],
                    }
                )
                print(f"[{i}/{len(drugs)}] {drug}: found={value is not None}, day={day_val}")
            except PlaywrightTimeoutError as e:
                rows.append(
                    {
                        "drug_name": drug,
                        "found": False,
                        "half_life_value": None,
                        "half_life_unit": "",
                        "half_life_day": None,
                        "response_text": f"timeout: {e}",
                    }
                )
                print(f"[{i}/{len(drugs)}] {drug}: timeout")
            except Exception as e:
                rows.append(
                    {
                        "drug_name": drug,
                        "found": False,
                        "half_life_value": None,
                        "half_life_unit": "",
                        "half_life_day": None,
                        "response_text": f"error: {e}",
                    }
                )
                print(f"[{i}/{len(drugs)}] {drug}: error {e}")

            # incremental save
            pd.DataFrame(rows).to_excel(out_xlsx, index=False)

        context.close()

    print(f"written {out_xlsx}")


if __name__ == "__main__":
    main()
