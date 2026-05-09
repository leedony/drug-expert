#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

# Canonical human Fc CH2-CH3 cores from UniProt IGHG1/2/3/4.
REFS = {
    "IgG1": "ELLGGPSVFLFPPKPKDTLMISRTPEVTCVVVDVSHEDPEVKFNWYVDGVEVHNAKTKPREEQYNSTYRVVSVLTVLHQDWLNGKEYKCKVSNKALPAPIEKTISKAKGQPREPQVYTLPPSRDELTKNQVSLTCLVKGFYPSDIAVEWESNGQPENNYKTTPPVLDSDGSFFLYSKLTVDKSRWQQGNVFSCSVMHEALHNHYTQKSLSLSP",
    "IgG2": "VAGPSVFLFPPKPKDTLMISRTPEVTCVVVDVSHEDPEVQFNWYVDGVEVHNAKTKPREEQFNSTFRVVSVLTVVHQDWLNGKEYKCKVSNKGLPAPIEKTISKTKGQPREPQVYTLPPSREEMTKNQVSLTCLVKGFYPSDISVEWESNGQPENNYKTTPPMLDSDGSFFLYSKLTVDKSRWQQGNVFSCSVMHEALHNHYTQKSLSLSP",
    "IgG3": "ELLGGPSVFLFPPKPKDTLMISRTPEVTCVVVDVSHEDPEVQFKWYVDGVEVHNAKTKPREEQYNSTFRVVSVLTVLHQDWLNGKEYKCKVSNKALPAPIEKTISKTKGQPREPQVYTLPPSREEMTKNQVSLTCLVKGFYPSDIAVEWESSGQPENNYNTTPPMLDSDGSFFLYSKLTVDKSRWQQGNIFSCSVMHEALHNRFTQKSLSLSP",
    "IgG4": "EFLGGPSVFLFPPKPKDTLMISRTPEVTCVVVDVSQEDPEVQFNWYVDGVEVHNAKTKPREEQFNSTYRVVSVLTVLHQDWLNGKEYKCKVSNKGLPSSIEKTISKAKGQPREPQVYTLPPSQEEMTKNQVSLTCLVKGFYPSDIAVEWESNGQPENNYKTTPPVLDSDGSFFLYSRLTVDKSRWQEGNVFSCSVMHEALHNHYTQKSLSLSL",
}
VALID = set("ACDEFGHIKLMNPQRSTVWY")


def clean_seq(value: object) -> str:
    if pd.isna(value):
        return ""
    return "".join(ch for ch in str(value).upper() if ch in VALID)


def best_match(query: str, ref: str) -> tuple[float, int, str]:
    # Allow small offset in extracted start/end positions.
    best: tuple[float, int, str] | None = None
    for off in range(-8, 9):
        if off >= 0:
            q = query[off:]
            r = ref[: len(q)]
        else:
            r = ref[-off:]
            q = query[: len(r)]
        n = min(len(q), len(r))
        if n < 120:
            continue
        q = q[:n]
        r = r[:n]
        ident = sum(1 for a, b in zip(q, r) if a == b) / n
        if best is None or ident > best[0]:
            best = (ident, off, q)
    if best is None:
        return 0.0, 0, ""
    return best


def mutation_calls(seq: str, subtype: str, off: int) -> str:
    ref = REFS[subtype]
    if off >= 0:
        seq_seg = seq[off : off + len(ref)]
        ref_seg = ref[: len(seq_seg)]
        start_pos = 1
    else:
        seq_seg = seq[: len(ref) + off]
        ref_seg = ref[-off : -off + len(seq_seg)]
        start_pos = 1 - off
    diffs: list[str] = []
    for i, (wt, obs) in enumerate(zip(ref_seg, seq_seg), start=start_pos):
        if wt != obs:
            diffs.append(f"{wt}{i}{obs}")
    return ";".join(diffs)


def renumber_mutations_to_eu(mutation_str: str, offset: int = 230) -> str:
    if not mutation_str:
        return ""
    out: list[str] = []
    for token in mutation_str.split(";"):
        t = token.strip()
        if len(t) < 3:
            continue
        wt = t[0]
        obs = t[-1]
        num = t[1:-1]
        if wt.isalpha() and obs.isalpha() and num.isdigit():
            out.append(f"{wt}{int(num) + offset}{obs}")
        else:
            out.append(t)
    return "; ".join(out)


def classify_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    pred: list[str] = []
    conf: list[str] = []
    ident_out: list[float | None] = []
    offset_out: list[int | None] = []
    muts: list[str] = []

    for raw in df.get("Fc_sequence_extracted", pd.Series([""] * len(df))):
        seq = clean_seq(raw)
        if len(seq) < 120:
            pred.append("unknown")
            conf.append("low")
            ident_out.append(None)
            offset_out.append(None)
            muts.append("")
            continue

        scores: dict[str, float] = {}
        offsets: dict[str, int] = {}
        for subtype, ref in REFS.items():
            ident, off, _ = best_match(seq, ref)
            scores[subtype] = ident
            offsets[subtype] = off

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        top_type, top_score = ranked[0]
        second_score = ranked[1][1]
        margin = top_score - second_score

        if top_score < 0.90:
            subtype = "unknown"
            confidence = "low"
            off = 0
            mut = ""
        else:
            subtype = top_type
            off = offsets[subtype]
            if top_score >= 0.985 and margin >= 0.01:
                confidence = "high"
            elif top_score >= 0.96 and margin >= 0.004:
                confidence = "medium"
            else:
                confidence = "low"
            mut = mutation_calls(seq, subtype, off)

        pred.append(subtype)
        conf.append(confidence)
        ident_out.append(round(float(top_score), 4))
        offset_out.append(off)
        muts.append(mut)

    out = df.copy()
    out["Fc_subtype_predicted"] = pred
    out["Fc_subtype_confidence"] = conf
    out["Fc_subtype_identity"] = ident_out
    out["Fc_alignment_offset"] = offset_out
    out["Fc_mutations_vs_predicted_wt"] = muts
    out["Fc_mutations_vs_predicted_wt_EU"] = [
        renumber_mutations_to_eu(x, offset=230) for x in muts
    ]
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Predict Fc subclass (IgG1/2/3/4) and call mutations vs subclass wild-type."
    )
    parser.add_argument("--input", required=True, help="Input xlsx path")
    parser.add_argument("--sheet", default="merged_filled", help="Input sheet name")
    parser.add_argument("--output", required=True, help="Output xlsx path")
    args = parser.parse_args()

    in_path = Path(args.input)
    out_path = Path(args.output)
    df = pd.read_excel(in_path, sheet_name=args.sheet)
    out = classify_dataframe(df)

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        out.to_excel(writer, sheet_name=args.sheet, index=False)

    print("Saved:", out_path)
    print(out["Fc_subtype_predicted"].value_counts(dropna=False))


if __name__ == "__main__":
    main()
