<div align="center">

# Ontology-Anything

**输入文档和表格，输出可执行本体。**
制度、SOP、合同、Excel → 对象类型、关系、角色和**带规则校验的动作**。每条规则都有原文引用，文件之间的冲突全部标出，人工批准前不生效。

[English](README.md) · MIT · Python ≥ 3.10 · 产物直接在 [nano-onto](https://github.com/JayLZhou/nano-ontology) 上运行

</div>

---

知识图谱工具抽的是**事实**（"S-003 是一家供应商"）。企业靠的是**规则**（"超过 5 万元的采购须财务总监审批"），而规则散落在多份文件里，彼此矛盾，还会随时间变化。Ontology-Anything 两样都抽，而且**不默认信任模型**：

```
 docs/*.md|pdf|docx ─┐                                      ┌─► review.md        （给人看）
                     ├─► 1 tables   → 类型、主键、关系、数据 ─┤
 data/*.csv|xlsx ────┘   2 enrich   → 标签、角色     (LLM)   ├─► draft.json       （给程序看）
                         3 extract  → 动作、规则、事实 (LLM) │
                         4 verify   → 引用必须在原文中逐字出现 └─► approve ─► ontology.yaml + seed.json
                         5 validate → 必须能被 nano-onto 编译                      │
                         6 conflict → 同一条规则，数字不一样？                      ▼
                                                                 nano-onto serve-mcp （agent 在其上行动）
```

| 大模型建本体的老问题 | Ontology-Anything 的做法 |
|---|---|
| 规则是编的 | 每条规则 / 事实必须附原文**引用**；引用在原文里找不到 → 标为 UNVERIFIED，默认不采纳 |
| 规则引用了不存在的字段 | 每个动作和规则都用 nano-onto **编译检查**，通不过 → INVALID |
| 模型输出格式不对 | 输出的**结构**同样不可信：类型错误、缺字段、非 JSON 的回答只会变成备注或 INVALID 条目，不会让程序崩溃 |
| 文件之间互相矛盾 | **冲突检测**：同一条件、不同阈值的规则会被配对，并根据文件日期给出建议 |
| "新规定下个月才生效" | `schedule` 解决方式：两条规则都保留，按生效日期用 `today()` 自动切换 |
| 表格里其实已经有 schema | 类型、主键、编号前缀、枚举、**外键 → 关系**全部确定性推断，不用大模型 |
| 文字里藏着数据变更（"S-003 列入黑名单"） | 作为**事实**抽出，与数据核对（记录存在、字段存在、值类型正确），批准时写入 seed |
| 本体没人负责 | 产物是草稿 + 审核报告；`approve` 是显式的、留痕的一步 |

## 安装

```bash
git clone https://github.com/JayLZhou/Ontology-Anything && cd Ontology-Anything
pip install -e .            # 会同时从 GitHub 安装 nano-onto
pip install -e ".[office]"  # 可选：支持 .xlsx / .pdf / .docx 输入
```

## Demo（不需要 API Key）

```bash
ontology-anything demo
```

示例输入在 [`examples/procurement`](examples/procurement)：一份采购制度、一份后发的通知（把财务总监审批门槛从 5 万改成 3 万，12 月 1 日生效，并拉黑供应商 S-003）、两张 CSV。大模型的回答**预先录制**在 `cassette.json` 里（故意放了一条编造引用的规则和一条引用不存在字段的规则），所以每次结果一致：

```
== 1. build from examples/procurement/docs and examples/procurement/data (recorded LLM answers)
loaded 2 documents, 2 tables
  tables: 2 object types, 1 links, 18 records
  enrich: 3 roles, 2 object types
  draft: 3 actions, 8 rules, 1 facts, 1 conflicts

== 2. approve without resolving conflicts (expected to stop)
not approved: unresolved conflicts:
  C1: cfo_over_50k: params.amount > 50000 -> approval:cfo (from docs/采购管理制度.md, effective 2025-01-01);
      cfo_over_30k: params.amount > 30000 -> approval:cfo (from docs/关于调整采购审批权限的通知.md, effective 2026-12-01)
      suggested: schedule

== 3. approve with the suggested resolutions
  C1: scheduled cfo_over_50k@2025-01-01 -> cfo_over_30k@2026-12-01
  left out rule:no_purchase_from_suspended: invalid (... Supplier has no property or link 'suspended')
  left out rule:warn_rating_c: quote not verified (use --include-unverified to keep)
  applied fact:1: Supplier S-003 {'blacklisted': True}
approved: 2 object types, 3 actions, 6 rules

== 4. run the result with nano-onto
  2026-11-24: 40000 元采购 -> pending_approval ['dept_manager']
  2026-12-02: 40000 元采购 -> pending_approval ['cfo', 'dept_manager']
  向 S-003（通知里拉黑的供应商）采购 -> denied: 不得向黑名单供应商采购
```

（输出有删节。加 `--out demo-out` 可以保留 `review.md`、`ontology.yaml` 和 `seed.json`。）

也就是说：

1. 发现 5 万 vs 3 万的冲突，建议 `schedule`；
2. 不解决冲突就拒绝批准；
3. 批准后自动剔除编造的和编译不过的规则，并把"S-003 拉黑"应用到数据上；
4. 交给 nano-onto 运行：11 月 24 日 4 万元采购只需部门经理批；12 月 2 日起还需财务总监；向 S-003 下单直接被拒。

## 用在你自己的文件上

```bash
export OA_API_KEY=sk-...                     # 任意 OpenAI 兼容接口
export OA_BASE_URL=https://api.deepseek.com/v1  OA_MODEL=deepseek-chat   # 可选

# 1. 生成草稿（输入可以是文件，也可以是文件夹）
ontology-anything build ./制度 ./导出数据 -o out --name procurement --llm record:out/cassette.json

# 2. 先读 out/review.md，再批准
ontology-anything approve out --resolve C1=keep:cfo_over_30k --drop rule:某条规则

# 3. 交给 nano-onto
nano-onto init --schema out/ontology.yaml --db out/nano_onto.db --seed out/seed.json
nano-onto serve-mcp --schema out/ontology.yaml --db out/nano_onto.db --actor alice --role employee
```

**`build`**

| 选项 | 含义 |
|---|---|
| `--llm openai`（默认） | 调用 `OA_BASE_URL`（默认 OpenAI）上的 `OA_MODEL`（默认 `gpt-4o-mini`） |
| `--llm record:<文件>` | 调用模型，并把每次回答存进 cassette |
| `--llm replay:<文件>` | 只从 cassette 回放，不发任何请求，适合测试、CI 和演示 |
| `--llm none` | 只处理表格：类型、主键、关系，不抽动作和规则 |
| `--parser docling` | PDF / DOCX / PPTX 走 [Docling](https://github.com/docling-project/docling) 做版面解析（`pip install ".[docling]"`）。内置解析器支持 .md/.txt/.csv/.tsv/.xlsx/.pdf/.docx/.html |
| `--today YYYY-MM-DD` | 判断"新规则是否已生效"所用的日期（默认今天） |

**`approve`**

| 选项 | 含义 |
|---|---|
| `--resolve C1=keep:<规则 id>` | 冲突中只保留一条规则，其余删除 |
| `--resolve C1=schedule` | 全部保留，各自按生效日期加上时间条件 |
| `--resolve C1=suggested` / `--accept-suggestions` | 对某个冲突 / 所有冲突采用建议 |
| `--drop rule:<id>`（也可用 `action:`、`fact:`、`role:`、`object_type:`） | 剔除某一条 |
| `--include-unverified` | 保留引用未能匹配的条目（比如 OCR 有噪声），请先人工读过 |

批准时一定会剔除 INVALID 条目；只要还有冲突没解决就拒绝继续；并检查最终本体能否编译。如果冲突的另一方本来就要被剔除，则无需再做决定。所有处理都记录在 `out/approval.log`。

**输出目录里的文件**

| 文件 | 由谁生成 | 内容 |
|---|---|---|
| `review.md` | `build` | 给人读的审核报告：每个条目的状态、原文引用和错误 |
| `draft.json` | `build` | 完整草稿，`approve` 的输入 |
| `ontology.draft.yaml` | `build` | 未批准的本体，便于 diff |
| `ontology.yaml`、`seed.json` | `approve` | nano-onto 加载的内容 |
| `approval.log` | `approve` | 保留、剔除、排期、应用了什么 |

## Python API

```python
from ontology_anything import load_corpus, build, approve, render_review
from ontology_anything.llm import OpenAICompatLLM

draft = build(load_corpus(["./制度", "./导出数据"]), OpenAICompatLLM(), name="procurement")
print(render_review(draft))
ontology, seed, log = approve(draft, accept_suggestions=True)
```

任何带 `complete(key, system, user) -> str` 方法的对象都可以当作模型传入，方便接入自己的客户端。

## 目前的局限

- 每份文档的每个分块都会发给模型，文档多时费用相应增加（见路线图）。
- 冲突检测只覆盖结果相同的单一比较阈值（如 `params.amount > 50000`），复合条件之间的重叠暂时发现不了。
- 日期识别支持 `YYYY-MM-DD` 和 `2026年12月1日` 两种写法；生效日期识别支持中文（`自…起`）和英文（`effective from …`）。
- 引用核对只忽略空白和全角 / 半角差异；转述过的引用会被标为 UNVERIFIED，这是有意为之。

## 路线图

- [ ] 长文档：按动作检索相关片段，而不是逐块全扫
- [ ] 阈值以外的冲突（条件重叠、拒绝 vs 审批），用 SMT 求解器检查
- [ ] 增量更新：新文件与已批准本体做 diff，提出补丁
- [ ] 从 agent 运行轨迹自动演化本体（反复被拒 / 人工覆盖 → 触发规则复审）
- [ ] Web 审核界面（逐条接受 / 修改 / 拒绝，旁边对照原文）
- [ ] Benchmark：公开制度文本上的规则抽取准确率

## 测试

```bash
pip install -e ".[dev]" && pytest
```

测试不需要联网，也不需要 API Key：模型调用要么从 cassette 回放，要么由本地桩服务应答。

MIT License.
