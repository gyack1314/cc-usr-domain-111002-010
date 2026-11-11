# 证据资料精确打包模块（packager）

把审查意图转成精确的资料选择集合，并在打包前后核对成员。模块只用 Playwright
驱动门户页面上的可见控件，不调用 `/__control`、`/api/action` 等运维/内部接口，
也不直接改写页面的选择集合（`selected`/`excluded`）。

## 两种互斥意图

| 模式 | 输入 | 语义 |
| --- | --- | --- |
| `explicit` | `ids` 明确编号集合 | 编号不存在即报告缺项，**不默默缩小集合** |
| `matching` | `query` 筛选条件 + `exclusions` 排除编号 | 绑定页面显示的查询签名、结果修订、匹配总数 |

两种模式不能混用，构造时即拒绝（`IntentError`）。条件模式的排除编号必须属于当前
筛选的匹配集合，否则条件失效并撤回。

```json
{"mode": "explicit", "ids": ["DOC-01", "DOC-09"]}
{"mode": "matching", "query": "review", "exclusions": ["DOC-07"]}
```

## 选择语义的关键区分

- **当前页全选**（三态复选框）只作用于当前页；**选择全部匹配项**是独立按钮，进入
  `matching` 模式。三态框的勾选/半选状态只用于演示，**成员事实只从逐行复选框读取**，
  绝不据三态框推断全部成员。
- 在全部匹配模式下取消当前页某一行，该编号进入**排除项**，而不是退回显式选择；
  翻页核对会验证排除项没有被重新勾回。
- 选择证据通过正常翻页（上一页/下一页）与逐行复选框、全选按钮建立。

## 三重核对（任一不一致即撤回，不点击生成、不凑数量）

1. **选择后**：翻全部页逐行读取的实际成员必须与计划集合完全相等；
2. **预览**：预览列出的成员编号、选择模式、查询签名、修订与计划完全相等才允许生成；
3. **生成后**：下载文件内的 `manifest` 必须与已确认预览一致，才交给下游。

撤回报告 `AbortReport` 输出：`reason`、`stage`、`planned`、多选 `extra`、漏选
`missing`、失效条件 `invalid_conditions` 以及撤回时的实时绑定 `binding`。触发条件
包括筛选变化、列表修订变化（如预览前新增记录）、预览出现未计划资料、工作区残留旧勾选。

## 中断与找回

- 中断发生在**预览之前**：台账保持 `planned/interrupted`，可对同一意图重新规划运行；
- 中断发生在**已生成之后**：凭台账记录的 `package_id`，从页面“已生成资料包”控件
  找回同包并再次下载校验，**不重复打包**（不产生新的 package 动作）。

## 目录与命令

```sh
# 端到端示例（两种意图、撤回场景、中断找回），产物在 outputs/examples/
python -I examples/run_examples.py

# CLI
python -m packager.cli --base-url http://127.0.0.1:8090 run intent.json
python -m packager.cli --base-url http://127.0.0.1:8090 recover intent.json

# 测试
python -m unittest discover -s tests -v
```

每次运行按意图指纹 `intent_key` 落盘到 `state/packager/<key>/`（示例用
`outputs/examples/`）：`run.json` 台账、`evidence/` 选择核对 JSON 与截图、
`package/资料包.json`、`storage-state.json`（含 package_id 的来源状态）。
打包文件名不参与成员判断，也不依赖附件字节断点续传。
