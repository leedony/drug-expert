# PubMed OA PDF 本地下载器（MVP）

一个最小可用工具：  
- 按关键词检索（示例：`asthma`）  
- 仅下载 Open Access 且可获取 PDF 的文献  
- 按分类目录保存到本地  
- 提供 Web 前端界面触发下载与查看文件

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
