# Responses ↔ Chat Completions 的工具桥接

覆盖来自 PR #42。这里记的是**桥接的契约与坑**，不是某次排查的过程。

## 为什么需要这一层

Responses 协议有两种 Chat Completions 没有的工具形态，必须在本层翻译：

| Responses | Chat Completions | 桥接做法 |
|---|---|---|
| `type=custom`（自由文本工具） | 没有对应形态 | 临时包成严格的 `{input: string}` 函数；回来还原成 `custom_tool_call` + 原始字符串 |
| `type=namespace`（工具分组） | 没有分组概念 | 子工具递归展开成平铺 `function`；重名改名；**回程还原客户端原名** |

Codex 的 `apply_patch` 就是 `custom`：它期望 `custom_tool_call` 与原始字符串
`input`。若按普通函数返回 JSON `arguments`，客户端报
`tool apply_patch invoked with incompatible payload` 并把调用标记为 aborted
（补丁不会执行）。

`namespace` 的坑在回程：`read_file_2` 这类名字是**我们为了避开重名造出来的**，
客户端不认识它。回程不还原，客户端下一轮按自己声明的名字回传工具结果就匹配不上。

## 契约

- 子工具优先保留原名；与顶层或其它子工具冲突时，后面的按稳定后缀改名。
- 历史里的 `function_call` / `custom_tool_call` 用同一套出站名（`tool_choice` 同理）。
- custom 的包装是**严格**的：只接受 `{"input": "<string>"}` 这一种形态，多键、
  非字符串、非法 JSON 一律拒绝，且**整体校验通过才暴露任何可执行调用**
  （避免只发出半截的并行调用）。
- 残缺的 custom 调用（`length` 截断、缺 `finish_reason`、异常 EOF、缺工具名）
  **不作为可执行调用发出**——宁可让客户端看到失败，也不能让它执行不完整的补丁。
- Chat 后端无法强制执行 Responses custom 工具的 grammar；grammar 原文作为
  工具描述里的指导文本保留，同时严格校验包装格式。

## 两条出口必须一致

桥接有两个出口：非流式的 `to_responses_object` 与流式的 `_StreamTranslator`。
**两者都要拿到 `bridge`**，否则同一个请求只因为 `stream` 标志不同，客户端就拿到
两套工具名（`read_file` vs `read_file_2`）。

这条踩过一次：原实现的非流式出口漏传了 `bridge`，而当时的测试是**手动**把 bridge
传进函数里的，测的是函数本身、不是生产调用点，所以全绿。现在有测试直接锚住调用
点，并有「两条路径还原出的名字必须一致」的对照断言。

## 验证

```bash
python -m unittest server.tests.test_namespace_compat server.tests.test_responses_api -q
```

另有端到端一条（真 socket）：`dev/verify_responses_e2e.py` 确认自定义工具往返
——出站包成 `{input: string}`、回程还原成 `custom_tool_call`。

实测（PR 作者）：Codex CLI 经生产链路连续完成两次 `apply_patch`，未使用 shell 兜底，
文件内容校验通过。
