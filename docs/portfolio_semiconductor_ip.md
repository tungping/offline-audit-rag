# 半导体 IP 技术情报模式作品说明

## 项目定位

Offline Auto Audit 原本是本地企业合规审计工具。`semiconductor_ip` 模式把同一套离线 RAG 工作流扩展到半导体专利/IP 技术情报初筛：输入 synthetic 专利文本、技术交底、产品说明或论文摘要，输出 claim chart、IP 风险项和 Markdown 分析报告。

本模式用于技术理解、研发知识管理和 IP 初筛展示，不构成法律意见、侵权结论或专利有效性判断。

## 面向岗位

- AI 应用开发工程师
- 本地大模型/RAG 应用工程师
- 半导体专利分析与技术情报岗位
- 数据分析自动化/知识管理工程师

## 能力对应

- 本地 AI 工作流：Ollama、ChromaDB、Streamlit 和 pytest 组合，不依赖云端 API。
- RAG 检索：企业合规规则和半导体 IP 规则使用独立规则目录与 collection。
- 结构化抽取：半导体模式输出 claim chart CSV、IP risk items CSV 和 Markdown 报告。
- 风险控制：报告包含免责声明，证据不足项会标记人工复核。
- 工程可测：模式选择、输出生成、历史统计和 WebUI worker 路径均有自动化测试覆盖。

## Demo 流程

1. 打开 WebUI，选择 `半导体专利/IP技术情报`。
2. 使用页面默认 SiC MOSFET synthetic demo，或上传 `examples/semiconductor/sample_sic_patent.txt`。
3. 系统检索 `config/semiconductor_ip_rules/` 中的半导体规则。
4. 本地模型生成结构化 JSON，系统校验后写出 CSV 和 Markdown。
5. 在 WebUI 查看并下载 claim chart、IP 风险项和分析报告。

CLI 演示：

```bash
uv run python app.py --mode semiconductor_ip
```

汇总半导体输出：

```bash
uv run python summarize_audits.py --mode semiconductor_ip --output-dir output --write output/ip_portfolio_summary.md
```

## 可展示材料

- `examples/semiconductor/sample_sic_patent.txt`
- `examples/semiconductor/sample_sic_product_brief.txt`
- `examples/semiconductor/sample_ip_claim_chart.csv`
- `examples/semiconductor/sample_ip_risk_items.csv`
- `examples/semiconductor/sample_ip_analysis_report.md`

