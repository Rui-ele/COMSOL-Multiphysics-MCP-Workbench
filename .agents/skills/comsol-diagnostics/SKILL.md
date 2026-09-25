---
name: comsol-diagnostics
description: 采集 COMSOL 模型概况，或执行 GPT 的检查清单，通过 MCP 读取指定范围的事实和完整报告分页。
---

# COMSOL 信息采集

## 连接与定位

用 `comsol_status` 检查连接，需要时连接已知 Server。通过 `model_list` / `model_discover` 定位模型并按需 `model_attach`。用模型和节点读取结果，将任务给定的查找条件解析成真实 tag 或路径。

## 选择读取方式

| 需要的信息 | 调用方式 |
| --- | --- |
| 初次问题的概况 | `diagnostic_collect`，附用户描述的现象 |
| 全部或指定参数 | `diagnostic_parameters_read` |
| 指定节点的属性、子节点、问题和选择 | `diagnostic_node_read` |
| 指定 tag 路径下的子树 | `diagnostic_tree_read`，按返回的 `traversal_scope` 确认覆盖范围 |
| 计划给出的具体 Java getter | `comsol_api_read`，传入实际对象访问链、方法和类型参数 |

`diagnostic_node_read` 的 `properties=None` 读取全部属性，`properties=[]` 列出属性名，指定列表则定向读取。子树工具遍历支持的 `feature` / `propertyGroup`；其他容器按计划用通用 API 读取。

概况采集返回用于定位问题的初始信息。已有检查清单时直接执行清单中的读取，保留每项要验证的判断。分析过去某次求解时，在报告中注明当前模型状态和历史日志各自的来源、时间。

## 读取完整报告

工具返回 `report_delivery` 后，按 `next_offset` 调用 `diagnostic_report_page(report_id, offset, max_chars=12000)`，依次拼接各页 `report_markdown`，直到 `complete` 为真，再执行下一项。

续页来自同一份报告快照；读取失败时保留已收到的部分和缺页范围。整轮报告采用 [AGENTS.md](../../../AGENTS.md) 的格式，具体字段见 [任务与报告约定](../../../docs/task-protocol.md)。
