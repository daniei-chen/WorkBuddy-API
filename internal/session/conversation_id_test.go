package session

import (
	"encoding/json"
	"strings"
	"testing"
)

// 会话头族的 conversationId（issue #193）：客户端给了就透传；**没给要派生一个
// 同会话稳定的值** —— 上游对部分账号灰度启用了渠道校验，出站缺 X-Conversation-ID
// 即判 11-128「conversation id missing」，被灰度到的账号每次被选中都失败。

func bodyWith(messages ...map[string]any) []byte {
	b, _ := json.Marshal(map[string]any{"messages": messages})
	return b
}

func userMsg(text string) map[string]any {
	return map[string]any{"role": "user", "content": text}
}

func TestConversationIDPrefersClientValue(t *testing.T) {
	body := bodyWith(
		map[string]any{"role": "system", "content": "sys"},
		userMsg("你好"),
	)
	// 顶层 conversationId 原值透传（不重新派生 —— 派生值会让后台按对话聚合的键
	// 与客户端自己记的对不上）。
	withField := func(key string, v any) []byte {
		var obj map[string]any
		_ = json.Unmarshal(body, &obj)
		obj[key] = v
		out, _ := json.Marshal(obj)
		return out
	}
	for _, key := range []string{"conversationId", "conversation_id"} {
		if got := ConversationIDForHeaders(withField(key, "client-conv-1"), "sess"); got != "client-conv-1" {
			t.Errorf("%s 没透传：got %q", key, got)
		}
	}
	// metadata 形态同样透传
	meta, _ := json.Marshal(map[string]any{
		"messages": []map[string]any{userMsg("你好")},
		"metadata": map[string]any{"conversationId": "meta-conv"},
	})
	if got := ConversationIDForHeaders(meta, ""); got != "meta-conv" {
		t.Errorf("metadata.conversationId 没透传：got %q", got)
	}
}

func TestConversationIDStableAcrossTurns(t *testing.T) {
	// 同一会话的多轮：首条 user 消息不变、末尾消息在变 → 必须派生同一个值，
	// 否则后台会把一个会话拆成几十条（这正是「稳定」要保住的语义）。
	first := bodyWith(
		map[string]any{"role": "system", "content": "sys"},
		userMsg("帮我改这个函数"),
	)
	second := bodyWith(
		map[string]any{"role": "system", "content": "sys"},
		userMsg("帮我改这个函数"),
		map[string]any{"role": "assistant", "content": "改好了"},
		userMsg("再优化一下"),
	)
	a := ConversationIDForHeaders(first, "")
	b := ConversationIDForHeaders(second, "")
	if a == "" || a != b {
		t.Fatalf("同会话两轮派生不一致：%q vs %q", a, b)
	}
	if len(a) != 32 || strings.ContainsFunc(a, func(r rune) bool { return !isHex(r) }) {
		t.Errorf("派生值形态不是 32 hex（B3 TraceId 也用它）：%q", a)
	}
}

func TestConversationIDDiffersAcrossConversations(t *testing.T) {
	a := ConversationIDForHeaders(bodyWith(userMsg("会话甲的第一句")), "")
	b := ConversationIDForHeaders(bodyWith(userMsg("会话乙的第一句")), "")
	if a == b {
		t.Errorf("不同会话派生出同一个值：%q —— 后台会把两条对话并成一条", a)
	}
}

func TestConversationIDFallbacks(t *testing.T) {
	// 无 user 消息（探测请求：只有 system）→ 回落会话键
	probe := bodyWith(map[string]any{"role": "system", "content": "You are a helpful assistant."},
		userMsg(""))
	withSess := ConversationIDForHeaders(probe, "sess-key")
	again := ConversationIDForHeaders(probe, "sess-key")
	if withSess == "" || withSess != again {
		t.Errorf("无 user 文本时没有按会话键稳定派生：%q vs %q", withSess, again)
	}
	if withSess == ConversationIDForHeaders(probe, "another-sess") {
		t.Error("不同会话键派生出了同一个值")
	}
	// 什么都没有 → 仍要给出一个非空的 32 hex（头族必发，校验才不会 11-128）
	bare := ConversationIDForHeaders(nil, "")
	if len(bare) != 32 {
		t.Errorf("空请求也要给出合法值，got %q", bare)
	}
}

func isHex(r rune) bool {
	return (r >= '0' && r <= '9') || (r >= 'a' && r <= 'f')
}
