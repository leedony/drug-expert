#!/usr/bin/env python3
"""
Generate multi-sheet tables + figures for executive reporting from:
  reports/antibody_classification_from_halflife_scope.xlsx
Outputs under: reports/boss_presentation/
"""
from __future__ import annotations

import re
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

ROOT = Path(__file__).resolve().parents[1]
IN_XLSX = ROOT / "reports" / "antibody_classification_from_halflife_scope.xlsx"
HALF_XLSX = ROOT / "reports" / "halflife_fc_master_table_elimination_merged_filled.xlsx"
OUT_DIR = ROOT / "reports" / "boss_presentation"


def strip_primary_label(x: object) -> str:
    if pd.isna(x):
        return "missing"
    s = str(x).strip()
    if "|" in s:
        s = s.split("|", 1)[0].strip()
    return s.strip().lower()


def parse_half_life_days(val: object) -> float:
    if pd.isna(val):
        return float("nan")
    s = str(val).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    m = re.search(r"(\d+(?:\.\d+)?)\s*days?\b", s, re.I)
    if m:
        return float(m.group(1))
    m = re.search(r"half[- ]life[^\d]{0,60}(\d+(?:\.\d+)?)\s*days?\b", s, re.I)
    if m:
        return float(m.group(1))
    return float("nan")


def coalesce_half_life(row: pd.Series) -> float:
    for c in ("half_life_Skill", "half_life_DailyMed", "half_life_DrugBank"):
        v = parse_half_life_days(row.get(c, np.nan))
        if np.isfinite(v):
            return float(v)
    return float("nan")


def load_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    drug = pd.read_excel(IN_XLSX, sheet_name="drug_level_from_half_scope")
    half = pd.read_excel(HALF_XLSX, sheet_name="merged_filled")
    return drug, half


def enrich(drug: pd.DataFrame) -> pd.DataFrame:
    d = drug.copy()
    d["has_fc_core"] = d["has_fc"].map(strip_primary_label)
    d["multispecific_core"] = d["is_multispecific"].map(strip_primary_label)
    d["reliability_core"] = d["classification_reliability"].map(strip_primary_label)
    d["hl_days"] = d.apply(coalesce_half_life, axis=1)
    d["has_hl"] = np.isfinite(d["hl_days"])
    return d


def cohort_overview(drug: pd.DataFrame, half: pd.DataFrame) -> pd.DataFrame:
    scope_n = half["Primary_name_clean"].nunique()
    classified_n = len(drug)
    half_keys = set(half["Primary_name_clean"].astype(str).str.strip())
    drug_keys = set(drug["Primary_name_clean"].astype(str).str.strip())
    unmatched = sorted(half_keys - drug_keys)
    rows = [
        ("scope_drugs_in_halflife_master", scope_n),
        ("classified_unique_drugs_in_output", classified_n),
        ("scope_minus_classified_unmatched", scope_n - classified_n),
        ("drugs_with_any_numeric_half_life_days", int(drug["has_hl"].sum())),
    ]
    df = pd.DataFrame(rows, columns=["metric", "value"])
    df.loc[len(df)] = ("unmatched_primary_name_clean_examples", "; ".join(unmatched[:20]))
    return df


def save_tables(writer: pd.ExcelWriter, drug: pd.DataFrame) -> None:
    cohort_overview(drug, pd.read_excel(HALF_XLSX, sheet_name="merged_filled")).to_excel(
        writer, sheet_name="00_cohort_overview", index=False
    )

    fc = drug["has_fc_core"].value_counts(dropna=False).rename_axis("has_fc_core").reset_index(name="count")
    fc["pct_of_drugs"] = (fc["count"] / len(drug) * 100).round(2)
    fc.to_excel(writer, sheet_name="01_fc_distribution", index=False)

    g = (
        drug.groupby("has_fc_core", dropna=False)["hl_days"]
        .agg(count_drugs="size", n_with_hl=lambda s: s.notna().sum(), mean="mean", median="median", min="min", max="max")
        .reset_index()
    )
    g.to_excel(writer, sheet_name="02_fc_half_life_summary", index=False)

    drug["modality_class"].value_counts(dropna=False).rename_axis("modality_class").reset_index(name="count").to_excel(
        writer, sheet_name="03_modality_distribution", index=False
    )

    pd.crosstab(drug["has_fc_core"], drug["multispecific_core"], margins=True).to_excel(
        writer, sheet_name="04_fc_x_multispecific"
    )

    pd.crosstab(drug["cytokine_target"], drug["cytokine_receptor_target"], margins=True).to_excel(
        writer, sheet_name="05_cytokine_x_receptor_target"
    )

    drug["cytokine_serum_tier"].fillna("(non_cytokine_or_unknown)").value_counts().rename_axis("tier").reset_index(
        name="count"
    ).to_excel(writer, sheet_name="06_cytokine_serum_tier", index=False)

    drug["receptor_cell_class"].fillna("(not_receptor_target_or_unknown)").value_counts().rename_axis("cell_class").reset_index(
        name="count"
    ).to_excel(writer, sheet_name="07_receptor_cell_class", index=False)

    drug["reliability_core"].value_counts(dropna=False).rename_axis("reliability_core").reset_index(name="count").to_excel(
        writer, sheet_name="08_classification_reliability", index=False
    )

    pd.crosstab(drug["modality_class"], drug["has_fc_core"], margins=True).to_excel(writer, sheet_name="09_modality_x_fc")

    hl_avail = (
        drug.groupby("has_fc_core", dropna=False)["has_hl"]
        .agg(n_true="sum", n_false=lambda s: (~s).sum())
        .reset_index()
    )
    hl_avail.to_excel(writer, sheet_name="10_half_life_coverage_by_fc", index=False)

    drug.groupby("modality_class", dropna=False)["hl_days"].agg(
        n_drugs="size", n_with_hl=lambda s: s.notna().sum(), median="median", mean="mean"
    ).reset_index().to_excel(writer, sheet_name="11_modality_half_life_summary", index=False)


def style_plots() -> None:
    sns.set_theme(style="whitegrid", context="talk", font_scale=0.85)
    plt.rcParams["figure.dpi"] = 120
    plt.rcParams["savefig.dpi"] = 220
    plt.rcParams["axes.titlesize"] = 13
    plt.rcParams["axes.labelsize"] = 11


def fig_cohort_bar(half: pd.DataFrame, drug: pd.DataFrame, out: Path) -> None:
    scope = half["Primary_name_clean"].nunique()
    classified = len(drug)
    with_hl = int(drug["has_hl"].sum())
    fig, ax = plt.subplots(figsize=(6, 4))
    sns.barplot(x=["Scope (halflife master)", "Classified (Cortellis matched)", "With numeric t1/2 (days)"], y=[scope, classified, with_hl], ax=ax, palette="Blues_r")
    for i, v in enumerate([scope, classified, with_hl]):
        ax.text(i, v + max(scope, 1) * 0.02, str(v), ha="center", va="bottom", fontsize=11)
    ax.set_ylabel("Count")
    ax.set_title("Cohort size: scope vs classified vs PK coverage")
    plt.xticks(rotation=15, ha="right")
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def fig_fc_counts(drug: pd.DataFrame, out: Path) -> None:
    s = drug["has_fc_core"].value_counts().reindex(["yes", "no", "unknown", "missing"]).fillna(0).astype(int)
    fig, ax = plt.subplots(figsize=(5.5, 4))
    sns.barplot(x=s.index.astype(str), y=s.values, ax=ax, palette="Set2")
    ax.set_xlabel("has_fc (primary token before '|')")
    ax.set_ylabel("Drugs")
    ax.set_title("Fc classification counts (drug level)")
    for i, v in enumerate(s.values):
        ax.text(i, v + max(s.values.max(), 1) * 0.02, str(int(v)), ha="center", va="bottom")
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def fig_halflife_by_fc(drug: pd.DataFrame, out: Path) -> None:
    sub = drug[drug["has_fc_core"].isin(["yes", "no", "unknown"])].copy()
    sub = sub[np.isfinite(sub["hl_days"])]
    order = ["yes", "no", "unknown"]
    fig, ax = plt.subplots(figsize=(7, 4.8))
    sns.boxplot(data=sub, x="has_fc_core", y="hl_days", order=order, ax=ax, showfliers=True)
    sns.stripplot(data=sub, x="has_fc_core", y="hl_days", order=order, ax=ax, color="0.25", alpha=0.35, size=3, jitter=0.22)
    ax.set_yscale("log")
    ax.set_ylabel("Half-life (days, log scale)")
    ax.set_xlabel("has_fc")
    ax.set_title("Distribution of half-life by Fc group (numeric + text-derived days)")
    for i, g in enumerate(order):
        n = (sub["has_fc_core"] == g).sum()
        ax.text(i, ax.get_ylim()[1] * 0.85, f"n={n}", ha="center", fontsize=10)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def fig_modality_counts(drug: pd.DataFrame, out: Path) -> None:
    vc = drug["modality_class"].value_counts()
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    sns.barplot(x=vc.values, y=vc.index.astype(str), ax=ax, palette="crest")
    ax.set_xlabel("Drugs")
    ax.set_ylabel("modality_class")
    ax.set_title("Modality mix (antibody architecture)")
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def fig_flags_faceted(drug: pd.DataFrame, out: Path) -> None:
    panels = [
        ("is_multispecific (primary token)", "multispecific_core"),
        ("cytokine_target", "cytokine_target"),
        ("cytokine_receptor_target", "cytokine_receptor_target"),
    ]
    fig, axes = plt.subplots(1, 3, figsize=(10.5, 3.8), sharey=False)
    for ax, (title, col) in zip(axes, panels):
        vc = drug[col].astype(str).str.lower().value_counts()
        sns.barplot(x=vc.values, y=vc.index.astype(str), ax=ax, palette="muted")
        ax.set_title(title)
        ax.set_xlabel("Drugs")
    fig.suptitle("Key target / format flags (separate panels to avoid label collision)", y=1.02)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)


def fig_halflife_by_modality(drug: pd.DataFrame, out: Path) -> None:
    sub = drug[np.isfinite(drug["hl_days"])].copy()
    order = sub["modality_class"].value_counts().index.tolist()
    fig, ax = plt.subplots(figsize=(8.5, 5))
    sns.boxplot(data=sub, x="modality_class", y="hl_days", order=order, ax=ax, showfliers=True)
    sns.stripplot(data=sub, x="modality_class", y="hl_days", order=order, ax=ax, color="0.25", alpha=0.25, size=2.5, jitter=0.2)
    ax.set_xticklabels(order, rotation=20, ha="right")
    ax.set_yscale("log")
    ax.set_ylabel("Half-life (days, log)")
    ax.set_xlabel("modality_class")
    ax.set_title("Half-life by modality (only drugs with parseable PK)")
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def fig_heatmap_modality_fc(drug: pd.DataFrame, out: Path) -> None:
    ct = pd.crosstab(drug["modality_class"], drug["has_fc_core"])
    fig, ax = plt.subplots(figsize=(6.5, 4.2))
    sns.heatmap(ct, annot=True, fmt="d", cmap="YlOrBr", ax=ax)
    ax.set_title("Counts: modality × has_fc (sanity check for consistency)")
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def fig_hl_coverage_stacked(drug: pd.DataFrame, out: Path) -> None:
    t = (
        drug.groupby(["has_fc_core", drug["has_hl"]])
        .size()
        .unstack(fill_value=0)
        .rename(columns={False: "no_PK_days", True: "has_PK_days"})
    )
    t = t.reindex(["yes", "no", "unknown"]).fillna(0).astype(int)
    fig, ax = plt.subplots(figsize=(6.2, 4.2))
    t.plot(kind="bar", stacked=True, ax=ax, color=["#c44e52", "#4c72b0"])
    ax.set_xlabel("has_fc")
    ax.set_ylabel("Drugs")
    ax.set_title("PK numeric coverage by Fc group (stacked)")
    ax.legend(title="Parseable t1/2")
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def fig_reliability(drug: pd.DataFrame, out: Path) -> None:
    vc = drug["reliability_core"].value_counts()
    fig, ax = plt.subplots(figsize=(5.5, 3.8))
    sns.barplot(x=vc.index.astype(str), y=vc.values, ax=ax, palette="flare")
    ax.set_xlabel("classification_reliability (primary token)")
    ax.set_ylabel("Drugs")
    ax.set_title("Rule confidence mix")
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def fig_receptor_cell(drug: pd.DataFrame, out: Path) -> None:
    s = drug["receptor_cell_class"].fillna("(not applicable / unknown)").value_counts().head(12)
    fig, ax = plt.subplots(figsize=(7.5, 4.6))
    sns.barplot(x=s.values, y=s.index.astype(str), ax=ax, palette="rocket")
    ax.set_xlabel("Drugs")
    ax.set_ylabel("receptor_cell_class (top 12)")
    ax.set_title("Cell-context classes for cytokine-receptor targeting subset")
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def write_readme(drug: pd.DataFrame, half: pd.DataFrame, out_md: Path) -> None:
    n = len(drug)
    nh = int(drug["has_hl"].sum())
    scope_n = int(half["Primary_name_clean"].nunique())
    text = f"""# 抗体分类与半衰期：汇报用图表与表格说明

本目录由脚本 `scripts/generate_boss_classification_report.py` 一键生成，便于向管理层说明**数据范围、分类结果分布、以及半衰期（PK）信息的覆盖度与组间差异**。

## 数据口径（老板最关心的三件事）

1. **分析对象是谁**：半衰期主表 `merged_filled` 中 **{scope_n}** 个 `Primary_name_clean` 作为 scope；其中能在 Cortellis 子集匹配并写出分类的 **{n}** 个药物进入 `drug_level_from_half_scope`。
2. **半衰期从哪来**：优先 `half_life_Skill` 的纯数字；否则从 `half_life_DailyMed` / `half_life_DrugBank` 文本中用正则抽取形如 `X days` 的数值；三者按此顺序**取第一个成功解析的值**合并为 `hl_days`。
3. **标签里的 `|`**：如 `yes | alternatives: unknown` 表示规则给出主结论同时存在替代解释；本报告统计 **`|` 之前的主标签**（`has_fc_core` 等），避免重复计数混乱。

当前可解析到数值半衰期的药物：**{nh} / {n}**（其余行不是“没有药”，而是三列均未解析出天数）。

---

## 表格（`boss_tables.xlsx`）对应关系与用途

| 工作表 | 用途 | 为什么适合汇报 |
| --- | --- | --- |
| `00_cohort_overview` | scope / 分类数 / PK 覆盖、未匹配示例 | 先对齐样本量，避免把“分类 unknown”误读成“数据缺失”。 |
| `01_fc_distribution` | Fc 主标签计数与占比 | 管理层快速看结构；条形/计数表最直观。 |
| `02_fc_half_life_summary` | 按 Fc 组的 t1/2 描述统计 | 用**中位数+极差**汇报偏态分布比单报均值更稳。 |
| `03_modality_distribution` | mAb / ADC / mAb+蛋白 等架构占比 | 解释管线技术形态结构。 |
| `04_fc_x_multispecific` | Fc × 多特异性交叉表 | 看两类重要标签是否独立或耦合。 |
| `05_cytokine_x_receptor_target` | 细胞因子靶 vs 细胞因子受体靶 | 展示免疫调节相关靶点的重叠结构。 |
| `06_cytokine_serum_tier` | 血清层级（仅细胞因子靶子集有意义） | 解释“系统暴露风险分层”的补充信息密度。 |
| `07_receptor_cell_class` | 受体靶相关的细胞语境类别 | 帮助生物学同事理解“受体在哪类细胞上读”。 |
| `08_classification_reliability` | 规则置信度 mix | 提醒哪些结论需要人工复核。 |
| `09_modality_x_fc` | modality × Fc 计数 | 一致性 sanity check（例如纯 mAb 与 Fc 关系）。 |
| `10_half_life_coverage_by_fc` | 各 Fc 组是否解析到 t1/2 | **避免**在“无 Fc 组样本极少 + PK 缺失多”的情况下过度解读均值。 |
| `11_modality_half_life_summary` | 各 modality 的 PK 覆盖与中位/均值 | 比较架构差异时同时看 **n_with_hl**。 |

---

## 图片文件与“为什么用这种图”

### `Fig01_cohort_scope_classified_pk.png`

- **类型**：简单柱状图。
- **目的**：一句话讲清 **scope → 可分类 → 有数值 PK** 的漏斗关系。
- **原因**：老板需要先确认“我们到底在讨论多少资产、其中多少真的有 PK 数字”，再进入组间比较。

### `Fig02_fc_counts.png`

- **类型**：条形图（类别计数）。
- **目的**：展示 `has_fc` 主标签（yes / no / unknown）的规模。
- **原因**：类别变量用条形图是标准选择；`unknown` 必须单独展示，避免 338+12 被误加为全量。

### `Fig03_halflife_by_fc_boxstrip_log.png`

- **类型**：箱线图 + 抖动散点，**Y 轴对数**。
- **目的**：比较不同 Fc 组的 t1/2 分布与离群值。
- **原因**：PK 通常右偏且跨度大；箱线图展示中位数与四分位；散点展示真实样本密度；对数轴压缩极端值，避免“一个 200 天”压扁整张图。

### `Fig04_modality_counts.png`

- **类型**：水平条形图。
- **目的**：展示 modality 结构（纯 mAb vs 带载荷/融合等）。
- **原因**：类别名较长，水平条更易读。

### `Fig05_flags_faceted_counts.png`

- **类型**：三个并列水平条形图（分面）。
- **目的**：并列展示多特异性、细胞因子靶、细胞因子受体靶等关键布尔型标签的规模。
- **原因**：若用单一分组条形图，不同维度的 `yes/no` 会在 Y 轴上“撞名”，容易误读；分面后每个维度独立坐标，更适合管理层扫读。

### `Fig06_halflife_by_modality_boxstrip_log.png`

- **类型**：箱线图 + 散点（对数轴）。
- **目的**：比较不同技术架构的 t1/2 分布（在可解析 PK 的子集内）。
- **原因**：与 Fc 图同理；同时提醒：组内 n 可能差异很大，应结合表格 `11_...` 看 `n_with_hl`。

### `Fig07_heatmap_modality_x_fc.png`

- **类型**：热力图（计数矩阵）。
- **目的**：快速发现“哪一格异常稀疏/异常密集”。
- **原因**：二维类别关系用热力图比堆叠条形更省空间；适合作为附录/答疑页。

### `Fig08_pk_coverage_stacked_by_fc.png`

- **类型**：堆叠条形图。
- **目的**：每个 Fc 组里，有多少药物**真的解析到了 t1/2**。
- **原因**：防止老板把“组间箱线图差异”误解为“全体药物都有 PK”；堆叠条直观表达缺失结构。

### `Fig09_reliability_counts.png`

- **类型**：条形图。
- **目的**：展示规则置信度分布。
- **原因**：分类不是 100% 金标准；先展示可靠性，再讲生物学结论更专业。

### `Fig10_receptor_cell_class_top12.png`

- **类型**：水平条形图（Top 类别）。
- **目的**：细胞语境维度信息密度高，取 Top 便于口头讲解。
- **原因**：类别过多时全量堆叠会噪音大，Top-N 更适合汇报主叙事。

---

## 建议的口头汇报顺序（5 分钟版）

1. `Fig01` + `00`：我们对齐样本与 PK 覆盖。
2. `Fig02` + `01`：Fc 不是只有 yes/no，有 **unknown**。
3. `Fig08` + `10`：再看每组有多少药能谈 PK。
4. `Fig03` + `02`：在可解析 PK 的前提下讨论分布（强调中位数/四分位）。
5. `Fig04`–`Fig06` + `03/11`：技术形态结构与 PK 关系是“第二层结论”。
6. `Fig09` + `08`：用可靠性收尾，提出需要专家复核的清单。

---
*生成文件列表：`boss_tables.xlsx`，`Fig01`…`Fig10`（文件名以目录为准）。*
"""
    out_md.write_text(text, encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    drug_raw, half = load_frames()
    drug = enrich(drug_raw)

    xlsx_out = OUT_DIR / "boss_tables.xlsx"
    with pd.ExcelWriter(xlsx_out, engine="openpyxl") as writer:
        save_tables(writer, drug)

    style_plots()
    fig_cohort_bar(half, drug, OUT_DIR / "Fig01_cohort_scope_classified_pk.png")
    fig_fc_counts(drug, OUT_DIR / "Fig02_fc_counts.png")
    fig_halflife_by_fc(drug, OUT_DIR / "Fig03_halflife_by_fc_boxstrip_log.png")
    fig_modality_counts(drug, OUT_DIR / "Fig04_modality_counts.png")
    fig_flags_faceted(drug, OUT_DIR / "Fig05_flags_faceted_counts.png")
    fig_halflife_by_modality(drug, OUT_DIR / "Fig06_halflife_by_modality_boxstrip_log.png")
    fig_heatmap_modality_fc(drug, OUT_DIR / "Fig07_heatmap_modality_x_fc.png")
    fig_hl_coverage_stacked(drug, OUT_DIR / "Fig08_pk_coverage_stacked_by_fc.png")
    fig_reliability(drug, OUT_DIR / "Fig09_reliability_counts.png")
    fig_receptor_cell(drug, OUT_DIR / "Fig10_receptor_cell_class_top12.png")

    write_readme(drug, half, OUT_DIR / "汇报说明_图表与表格.md")
    print("Wrote:", xlsx_out)
    print("Wrote figures and README under:", OUT_DIR)


if __name__ == "__main__":
    main()
