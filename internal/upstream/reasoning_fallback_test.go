package upstream

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"
)

// 思考通道兜底（issue #135）：国际版 deepseek 思考模式下正文只在
// reasoning_content 里，只看 content 的客户端会显示空回复。
//
// 契约（刻意很窄）：
//   - 整轮**既无正文、也无工具调用** → 把思考原文同时作为 content 下发；
//   - 有正文 / 有工具调用的正常轮次 → 零改动；
//   - 开关关闭 → 与旧行为逐字节一致；
//   - **帧序**：补发的正文必须早于 finish_reason（客户端看到 finish 就停止累积）。
func withReasoningFallback(t *testing.T, on bool) {
	t.Helper()
	old := ReasoningFallbackEnabled()
	ConfigureReasoningFallback(on)
	t.Cleanup(func() { ConfigureReasoningFallback(old) })
}

func sseFrame(t *testing.T, delta map[string]any) string {
	t.Helper()
	raw, err := json.Marshal(map[string]any{
		"id": "chatcmpl-x", "object": "chat.completion.chunk", "created": 1,
		"choices": []any{map[string]any{"index": 0, "delta": delta}},
	})
	if err != nil {
		t.Fatalf("marshal frame: %v", err)
	}
	return "data: " + string(raw)
}

func messageOf(t *testing.T, resp map[string]any) map[string]any {
	t.Helper()
	choices, _ := resp["choices"].([]any)
	if len(choices) == 0 {
		t.Fatalf("no choices in %v", resp)
	}
	c, _ := choices[0].(map[string]any)
	msg, _ := c["message"].(map[string]any)
	if msg == nil {
		t.Fatalf("no message in %v", c)
	}
	return msg
}

func TestAggregateBackfillsReasoningAsContent(t *testing.T) {
	withReasoningFallback(t, true)
	raw := strings.Join([]string{
		sseFrame(t, map[string]any{"role": "assistant", "reasoning_content": "思考A"}),
		"",
		sseFrame(t, map[string]any{"reasoning_content": "思考B"}),
		"",
		"data: [DONE]",
		"",
	}, "\n")

	resp, err := Aggregate(strings.NewReader(raw))
	if err != nil {
		t.Fatalf("aggregate: %v", err)
	}
	msg := messageOf(t, resp)
	if got := msg["content"]; got != "思考A思考B" {
		t.Errorf("content=%v want 思考A思考B（整轮无正文时应回填思考原文）", got)
	}
	// 思考原文仍保留：客户端要自己渲染时不能丢数据
	if got := msg["reasoning_content"]; got != "思考A思考B" {
		t.Errorf("reasoning_content=%v want 思考A思考B（回填不应删掉原字段）", got)
	}
}

func TestAggregateKeepsContentWhenPresent(t *testing.T) {
	withReasoningFallback(t, true)
	raw := strings.Join([]string{
		sseFrame(t, map[string]any{"reasoning_content": "思考"}),
		"",
		sseFrame(t, map[string]any{"content": "正文"}),
		"",
		"data: [DONE]",
		"",
	}, "\n")
	resp, err := Aggregate(strings.NewReader(raw))
	if err != nil {
		t.Fatalf("aggregate: %v", err)
	}
	if got := messageOf(t, resp)["content"]; got != "正文" {
		t.Errorf("content=%v want 正文（有正文时不得改写）", got)
	}
}

func TestAggregateSkipsBackfillWhenToolCalls(t *testing.T) {
	withReasoningFallback(t, true)
	raw := strings.Join([]string{
		sseFrame(t, map[string]any{"reasoning_content": "思考"}),
		"",
		sseFrame(t, map[string]any{"tool_calls": []any{map[string]any{
			"index": 0, "id": "call_1", "type": "function",
			"function": map[string]any{"name": "f", "arguments": "{}"},
		}}}),
		"",
		"data: [DONE]",
		"",
	}, "\n")
	resp, err := Aggregate(strings.NewReader(raw))
	if err != nil {
		t.Fatalf("aggregate: %v", err)
	}
	msg := messageOf(t, resp)
	if got := msg["content"]; got != "" {
		t.Errorf("content=%v want 空（有工具调用时不得把思考当正文，否则客户端会多显示一段"+
			"并可能误判为普通文本回答）", got)
	}
}

func TestAggregateFallbackCanBeDisabled(t *testing.T) {
	withReasoningFallback(t, false)
	raw := strings.Join([]string{
		sseFrame(t, map[string]any{"reasoning_content": "思考"}),
		"",
		"data: [DONE]",
		"",
	}, "\n")
	resp, err := Aggregate(strings.NewReader(raw))
	if err != nil {
		t.Fatalf("aggregate: %v", err)
	}
	msg := messageOf(t, resp)
	if got := msg["content"]; got != "" {
		t.Errorf("content=%v want 空（开关关闭时保持旧行为）", got)
	}
}

// 流式：补发的正文必须出现在 finish_reason **之前** —— 很多 OpenAI 兼容客户端
// 收到 finish 就停止累积，补在后面的正文等于没发（2.2.0 的回归点）。
func TestStreamBackfillsReasoningAsContent(t *testing.T) {
	withReasoningFallback(t, true)
	raw := strings.Join([]string{
		sseFrame(t, map[string]any{"role": "assistant", "reasoning_content": "思考A"}),
		"",
		sseFrame(t, map[string]any{"reasoning_content": "思考B"}),
		"",
		`data: {"id":"chatcmpl-x","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}`,
		"",
		"data: [DONE]",
		"",
	}, "\n")
	rec := httptest.NewRecorder()
	if err := StreamHint(rec, strings.NewReader(raw), nil); err != nil {
		t.Fatalf("stream: %v", err)
	}
	out := rec.Body.String()
	contentPos := strings.Index(out, `"content":"思考A思考B"`)
	finishPos := strings.Index(out, `"finish_reason":"stop"`)
	donePos := strings.Index(out, "[DONE]")
	if contentPos < 0 {
		t.Fatalf("流里没有补发的正文帧：\n%s", out)
	}
	if finishPos < 0 || contentPos > finishPos {
		t.Errorf("补发正文必须早于 finish_reason（content=%d finish=%d done=%d）：\n%s",
			contentPos, finishPos, donePos, out)
	}
	if finishPos > donePos {
		t.Errorf("finish 出现在 [DONE] 之后：\n%s", out)
	}
}

// finish 之后常跟一帧纯 usage（choices 为空）。它也要跟着缓冲，不能在补发正文之前
// 漏出去 —— 否则客户端同样会认为「这一轮结束了」。
func TestStreamBackfillKeepsUsageAfterFinish(t *testing.T) {
	withReasoningFallback(t, true)
	raw := strings.Join([]string{
		sseFrame(t, map[string]any{"reasoning_content": "思考"}),
		"",
		`data: {"id":"chatcmpl-x","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}`,
		"",
		`data: {"id":"chatcmpl-x","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":2}}`,
		"",
		"data: [DONE]",
		"",
	}, "\n")
	rec := httptest.NewRecorder()
	if err := StreamHint(rec, strings.NewReader(raw), nil); err != nil {
		t.Fatalf("stream: %v", err)
	}
	out := rec.Body.String()
	contentPos := strings.Index(out, `"content":"思考"`)
	finishPos := strings.Index(out, `"finish_reason":"stop"`)
	// 用 usage 帧里的实际字段定位：normalizeFrame 会给**每一帧**补 "usage":null，
	// 直接找 `"usage":` 会命中第一帧。
	usagePos := strings.Index(out, `"completion_tokens":2`)
	donePos := strings.Index(out, "[DONE]")
	if contentPos < 0 || usagePos < 0 || finishPos < 0 {
		t.Fatalf("missing frames: content=%d finish=%d usage=%d\n%s",
			contentPos, finishPos, usagePos, out)
	}
	if !(contentPos < finishPos && finishPos < usagePos && usagePos < donePos) {
		t.Errorf("帧序应为 content → finish → usage → [DONE]（%d/%d/%d/%d）：\n%s",
			contentPos, finishPos, usagePos, donePos, out)
	}
}

func TestStreamKeepsNormalTurnsUntouched(t *testing.T) {
	withReasoningFallback(t, true)
	raw := strings.Join([]string{
		sseFrame(t, map[string]any{"reasoning_content": "思考"}),
		"",
		sseFrame(t, map[string]any{"content": "正文"}),
		"",
		`data: {"id":"chatcmpl-x","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}`,
		"",
		"data: [DONE]",
		"",
	}, "\n")
	rec := httptest.NewRecorder()
	if err := StreamHint(rec, strings.NewReader(raw), nil); err != nil {
		t.Fatalf("stream: %v", err)
	}
	out := rec.Body.String()
	// 有正文时不得再补一帧把思考当正文（否则客户端显示两遍）
	if strings.Contains(out, `"content":"思考"`) {
		t.Errorf("有正文的轮次被补发了思考帧：\n%s", out)
	}
	// 正常轮次的 finish 与 DONE 仍在
	finishPos := strings.Index(out, `"finish_reason":"stop"`)
	donePos := strings.Index(out, "[DONE]")
	if finishPos < 0 || donePos < 0 || finishPos > donePos {
		t.Errorf("正常轮次的收尾帧丢了：\n%s", out)
	}
}

func TestStreamBackfillCanBeDisabled(t *testing.T) {
	withReasoningFallback(t, false)
	raw := strings.Join([]string{
		sseFrame(t, map[string]any{"reasoning_content": "思考"}),
		"",
		`data: {"id":"chatcmpl-x","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}`,
		"",
		"data: [DONE]",
		"",
	}, "\n")
	rec := httptest.NewRecorder()
	if err := StreamHint(rec, strings.NewReader(raw), nil); err != nil {
		t.Fatalf("stream: %v", err)
	}
	if strings.Contains(rec.Body.String(), `"content":"思考"`) {
		t.Errorf("开关关闭时仍然补发了正文：\n%s", rec.Body.String())
	}
}
