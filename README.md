# PubMed OA PDF 本地下载器（MVP）

一个最小可用工具：  
- 按关键词检索（示例：`asthma`）  
- 仅下载 Open Access 且可获取 PDF 的文献  
- 按分类目录保存到本地  
- 提供 Web 前端界面触发下载与查看文件

## 最小可运行指南（3步）

### 1) 安装依赖

```bash
python3 -m pip install -r requirements.txt
```

### 2) 一键跑通（下载 + 半衰期提取）

```bash
python3 - <<'PY'
from app import api_download
from pathlib import Path
from analyze_half_life import analyze_drug_folder
import pandas as pd

drug_name = "bimekizumab"   # 可替换为任意药物
max_results = 12

r = api_download(term=drug_name, max_results=max_results, category=drug_name)
print("downloaded:", r.get("downloaded_count"), "deduped:", r.get("deduped_count"), "failed:", r.get("failed_count"))

result = analyze_drug_folder(Path("downloads") / drug_name)
print("half_life_result:", result)
Path("reports").mkdir(exist_ok=True)
pd.DataFrame([result]).to_excel(Path("reports") / f"{drug_name}_half_life_report.xlsx", index=False)
print("saved:", Path("reports") / f"{drug_name}_half_life_report.xlsx")
PY
```

### 3) 验证输出

- 下载文件夹：`downloads/<drug_name>/`
- 半衰期报告：`reports/<drug_name>_half_life_report.xlsx`
- 关键字段：`half_life_value`、`half_life_unit`、`half_life_hours`、`source_file`、`evidence`

> 若只想看 Web 界面，再执行 `python3 app.py` 并访问 `http://127.0.0.1:8000`。

## 1. 安装依赖

```bash
python3 -m pip install -r requirements.txt
```

## 2. 启动服务

```bash
python3 app.py
```

默认地址：`http://127.0.0.1:8000`

## 3. 使用方式

打开页面后：
- 检索词填写 `asthma`
- 设置最多下载条数（建议先 5~20）
- 设置分类目录（例如 `asthma`）
- 点击“开始下载 OA PDF”

下载结果会显示在页面下方，并可直接点击文件链接打开本地 PDF。

## 4. API

- `GET /api/search?term=asthma&max_results=20`  
  查询 PubMed 元数据（不下载）

- `POST /api/download?term=asthma&max_results=20&category=asthma`  
  下载 OA PDF 到 `downloads/<category>/`

- `GET /api/files`  
  查看本地已下载 PDF 列表

## 5. 下载策略说明

当前下载优先走 PMC AWS 公共数据链接（官方云分发渠道），如果不可用则回退到 PMC OA API 链接解析。  
仅当文件头校验为 `%PDF-` 才会保留，避免将 HTML/错误页误存为 PDF。
