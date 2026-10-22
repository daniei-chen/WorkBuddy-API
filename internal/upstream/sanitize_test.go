package upstream

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"workbuddy2api/internal/auth"
)

const (
	ccIdentity = "You are Claude Code, Anthropic's official CLI for Claude."
	ccBranch   = "Main branch (you will usually use this for PRs)"
	ccHeader   = "x-anthropic-billing-header: cc_version=1.0; cc_entrypoint=cli;"
	// Codex instructions 首段（上游逐字精确指纹，三句缺一不可）。
	codexInstructions = "You are a coding agent running in the Codex CLI, a terminal-based coding assistant. Codex CLI is an open source project led by OpenAI. You are expected to be precise, safe, and helpful."
)

func TestIdentityRewritten(t *testing.T) {
	out := sanitizeText(ccIdentity)
	if !strings.Contains(out, "official CLI tool for Claude.") {
		t.Errorf("identity not rewritten: %q", out)
	}
	if strings.Contains(out, ccIdentity) {
		t.Errorf("original identity still present: %q", out)
	}
}

// 桌面版（claude-desktop-3p / Agent SDK）的身份句以逗号接后继内容，结尾不是句号。
// 回归用例：匹配串曾带结尾句号，导致该形态漏网、指纹原样发上游 → 400 code=11128。
func TestIdentityDesktopVariantRewritten(t *testing.T) {
	in := "You are Claude Code, Anthropic's official CLI for Claude, running within the Claude Agent SDK."
	out := sanitizeText(in)
	if strings.Contains(out, "official CLI for Claude") {
		t.Errorf("desktop identity not rewritten: %q", out)
	}
	if !strings.Contains(out, "official CLI tool for Claude, running within the Claude Agent SDK.") {
		t.Errorf("desktop identity suffix not preserved: %q", out)
	}
}

func TestBranchRewritten(t *testing.T) {
	out := sanitizeText(ccBranch)
	if !strings.Contains(out, "Default branch (you will usually use this for PRs)") {
		t.Errorf("branch not rewritten: %q", out)
	}
	if strings.Contains(out, "Main branch") {
		t.Errorf("original branch still present: %q", out)
	}
}

// 反馈句带 Anthropic 仓库链接，上游按整句拦截（实测只留链接或只留半边均不拦）。
// 回归用例：give→provide 一词之差即可绕过。
func TestFeedbackSentenceRewritten(t *testing.T) {
	in := "To give feedback, users should report the issue at https://github.com/anthropics/claude-code/issues"
	out := sanitizeText(in)
	if strings.Contains(out, "To give feedback") {
		t.Errorf("feedback sentence not rewritten: %q", out)
	}
	if !strings.Contains(out, "To provide feedback, users should report the issue at https://github.com/anthropics/claude-code/issues") {
		t.Errorf("feedback sentence not rewritten as expected: %q", out)
	}
}

// 上游反探测：请求体里出现裸数字 11128 即整单拦截（与上下文无关）。
// 回归用例：该串会被改写为 11-128 以打断精确匹配。
func TestUpstreamErrorCodeRewritten(t *testing.T) {
	in := "upstream returned code=11128 for this request"
	out := sanitizeText(in)
	if strings.Contains(out, "11128") {
		t.Errorf("error code not rewritten: %q", out)
	}
	if !strings.Contains(out, "11-128") {
		t.Errorf("error code not rewritten as expected: %q", out)
	}
}

// 回归：工具调用消息的 content 常为 null，而旧版 sanitizeMessages 在 content 缺失时
// 直接 continue，整条消息连 tool_calls 一起被跳过 → arguments 里的被拦字符串原样漏出。
func TestToolCallArgumentsSanitized(t *testing.T) {
	msgs := []any{
		map[string]any{"role": "user", "content": "run"},
		map[string]any{"role": "assistant", "content": nil, "tool_calls": []any{
			map[string]any{"id": "c1", "type": "function", "function": map[string]any{
				"name":      "Bash",
				"arguments": `{"command":"echo 11128"}`,
			}},
		}},
	}
	if !sanitizeMessages(msgs) {
		t.Fatal("sanitizeMessages 未报告任何改动，tool_calls 被跳过")
	}
	fn := msgs[1].(map[string]any)["tool_calls"].([]any)[0].(map[string]any)["function"].(map[string]any)
	got := fn["arguments"].(string)
	if strings.Contains(got, "11128") {
		t.Errorf("tool_call arguments 未被净化: %q", got)
	}
}

func TestBillingHeaderStrippedValueIrrelevant(t *testing.T) {
	out := sanitizeText(ccHeader)
	if strings.Contains(out, "x-anthropic-billing-header") {
		t.Errorf("header not stripped: %q", out)
	}
}

// 附加验证：正常对话里出现 github.com/anthropics/ 链接（但不是反馈句整句）
// 时，预检特征命中（进入净化），但改写层只动精确匹配的整句——普通链接文本
// 不该被改写。同理，既不含 11128 也不含反馈整句的文本原样返回。
func TestNormalAnthropicLinkNotRewritten(t *testing.T) {
	in := "see https://github.com/anthropics/anthropic-cookbook for examples"
	if out := sanitizeText(in); out != in {
		t.Errorf("normal anthropic link should be untouched: %q -> %q", in, out)
	}
}

// Codex instructions 首段：命中预告且整句改写，逐字指纹被破坏、语义保留。
func TestCodexInstructionsRewritten(t *testing.T) {
	out := sanitizeText(codexInstructions)
	if strings.Contains(out, codexInstructions) {
		t.Errorf("codex fingerprint still present: %q", out)
	}
	if !strings.Contains(out, "You are a coding agent running in the Codex CLI tool, a terminal-based coding assistant.") {
		t.Errorf("codex first sentence not rewritten: %q", out)
	}
	// 其余两句原样保留，语义不变。
	if !strings.Contains(out, "Codex CLI is an open source project led by OpenAI.") ||
		!strings.Contains(out, "You are expected to be precise, safe, and helpful.") {
		t.Errorf("codex remaining sentences altered: %q", out)
	}
}

// 预检必须能认出 Codex 特征（此前只含 Claude Code，导致提前放行）。
func TestCodexFingerprintDetected(t *testing.T) {
	if !hasFingerprint(codexInstructions) {
		t.Error("codex fingerprint not detected by precheck")
	}
}

// 非精确变体不应被改写：仅去掉其中一个词即视为已破坏，无需改动。
func TestCodexVariantNotTouched(t *testing.T) {
	in := "You are a coding agent running in a CLI, a terminal-based coding assistant."
	if out := sanitizeText(in); out != in {
		t.Errorf("already-broken variant should be untouched: %q -> %q", in, out)
	}
}

func TestBillingHeaderCaseInsensitive(t *testing.T) {
	alt := "X-Anthropic-Billing-Header: cc_version=1.0;"
	out := strings.ToLower(sanitizeText(alt))
	if strings.Contains(out, "billing") {
		t.Errorf("case-insensitive header not stripped: %q", out)
	}
}

func TestTrailingKVStripped(t *testing.T) {
	out := sanitizeText("...; cc_version=2.0; cc_entrypoint=cli;")
	if strings.Contains(out, "cc_version") || strings.Contains(out, "cc_entrypoint") {
		t.Errorf("trailing kv not stripped: %q", out)
	}
}

func TestExactMatchOnlyVariantNotTouched(t *testing.T) {
	in := "...official CLI for Claude!"
	if out := sanitizeText(in); out != in {
		t.Errorf("variant should be untouched: %q -> %q", in, out)
	}
}

func TestUserFreeTextNotTouched(t *testing.T) {
	in := "please use main branch for this repo"
	if out := sanitizeText(in); out != in {
		t.Errorf("free text should be untouched: %q -> %q", in, out)
	}
}

func TestNoFeatureReturnsSameString(t *testing.T) {
	in := "ordinary user message"
	if out := sanitizeText(in); out != in {
		t.Errorf("no-feature text should pass through unchanged: %q -> %q", in, out)
	}
}

func TestMultimodalTextPartOnly(t *testing.T) {
	imgPart := map[string]any{"type": "image", "source": map[string]any{"type": "base64", "data": "..."}}
	content := []any{
		map[string]any{"type": "text", "text": ccIdentity},
		imgPart,
	}
	out, changed := sanitizeContent(content)
	if !changed {
		t.Fatal("expected change")
	}
	parts := out.([]any)
	txt, _ := parts[0].(map[string]any)["text"].(string)
	if !strings.Contains(txt, "CLI tool") {
		t.Errorf("text part not sanitized: %q", txt)
	}
	img, _ := parts[1].(map[string]any)
	if img["type"] != "image" || img["source"].(map[string]any)["data"] != "..." {
		t.Error("image part modified")
	}
}

// 集成：完整请求体经 PrepareBodyOpt 净化后无残留指纹，且 stream/tool_choice 行为不受影响。
func TestPrepareBodyOptSanitizesSystem(t *testing.T) {
	body := []byte(`{"model":"glm-5.2","messages":[` +
		`{"role":"system","content":"` + ccIdentity + ` ` + ccHeader + `"},` +
		`{"role":"user","content":"hi"}]}`)
	out := PrepareBodyOpt(body, true)
	var obj map[string]any
	if err := json.Unmarshal(out, &obj); err != nil {
		t.Fatal(err)
	}
	if obj["stream"] != true {
		t.Error("stream not forced")
	}
	msgs := obj["messages"].([]any)
	sys, _ := msgs[0].(map[string]any)["content"].(string)
	if strings.Contains(sys, "x-anthropic-billing-header") || strings.Contains(sys, ccIdentity) || strings.Contains(sys, ccBranch) {
		t.Errorf("fingerprints remain: %q", sys)
	}
	if !strings.Contains(sys, "CLI tool") {
		t.Errorf("rewrite missing: %q", sys)
	}
}

func TestPrepareBodyOptDisabledPreservesFingerprints(t *testing.T) {
	body := []byte(`{"model":"glm-5.2","messages":[{"role":"system","content":"` + ccIdentity + `"}]}`)
	out := PrepareBodyOpt(body, false)
	if !strings.Contains(string(out), ccIdentity) {
		t.Error("sanitize=false should preserve fingerprints")
	}
	// 但 stream 仍强制
	var obj map[string]any
	_ = json.Unmarshal(out, &obj)
	if obj["stream"] != true {
		t.Error("stream should still be forced")
	}
}

// PrepareBody 默认行为 = 开启脱敏（保持向后兼容）。
func TestPrepareBodyDefaultSanitizes(t *testing.T) {
	body := []byte(`{"model":"glm-5.2","messages":[{"role":"system","content":"` + ccIdentity + `"}]}`)
	out := PrepareBodyOpt(body, true)
	if strings.Contains(string(out), ccIdentity) {
		t.Error("PrepareBody default should sanitize")
	}
}

// 出站边界集成：ChatStream 发往上游的 wire body 必须无残留指纹。
func TestChatStreamWireBodySanitized(t *testing.T) {
	var gotBody []byte
	ts := newTestUpstream(t, func(w http.ResponseWriter, r *http.Request) {
		gotBody, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = w.Write([]byte("data: {\"choices\":[{\"delta\":{\"content\":\"ok\"}}]}\n\ndata: [DONE]\n\n"))
	})
	defer ts.Close()

	c := New()
	c.SanitizeFingerprints = true
	c.ChatBaseCN = ts.URL
	acct := &auth.Auth{AccessToken: "test-token", Domain: "copilot.tencent.com", UID: "u1"}

	body := []byte(`{"model":"glm-5.2","messages":[` +
		`{"role":"system","content":"` + ccIdentity + ` ` + ccHeader + `"},` +
		`{"role":"user","content":"hi"}]}`)
	rc, status, respBody, err := c.ChatStream(acct, body, "", ChatMeta{})
	if err != nil {
		t.Fatal(err)
	}
	defer rc.Close()
	if status >= 400 {
		t.Fatalf("upstream status %d: %s", status, respBody)
	}
	// 上游收到的 body：stream 强制 + 指纹已净化
	var obj map[string]any
	if err := json.Unmarshal(gotBody, &obj); err != nil {
		t.Fatalf("wire body not json: %v", err)
	}
	if obj["stream"] != true {
		t.Error("wire body stream not forced")
	}
	sys, _ := obj["messages"].([]any)[0].(map[string]any)["content"].(string)
	for _, fp := range []string{"x-anthropic-billing-header", ccIdentity, ccBranch} {
		if strings.Contains(sys, fp) {
			t.Errorf("wire body contains fingerprint %q: %q", fp, sys)
		}
	}
}

// 出站边界（Codex 场景）：CPA 把 instructions 折成 role=system 的首条消息，
// 净化后 wire body 不得残留 Codex 逐字指纹；其余消息不受影响。
func TestChatStreamWireBodyCodexInstructionsSanitized(t *testing.T) {
	var gotBody []byte
	ts := newTestUpstream(t, func(w http.ResponseWriter, r *http.Request) {
		gotBody, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = w.Write([]byte("data: {\"choices\":[{\"delta\":{\"content\":\"ok\"}}]}\n\ndata: [DONE]\n\n"))
	})
	defer ts.Close()

	c := New()
	c.SanitizeFingerprints = true
	c.ChatBaseCN = ts.URL
	acct := &auth.Auth{AccessToken: "test-token", Domain: "copilot.tencent.com", UID: "u1"}

	body := []byte(`{"model":"kimi-k3","messages":[` +
		`{"role":"system","content":"` + codexInstructions + `"},` +
		`{"role":"user","content":"say ok"}]}`)
	rc, status, respBody, err := c.ChatStream(acct, body, "", ChatMeta{})
	if err != nil {
		t.Fatal(err)
	}
	defer rc.Close()
	if status >= 400 {
		t.Fatalf("upstream status %d: %s", status, respBody)
	}
	var obj map[string]any
	if err := json.Unmarshal(gotBody, &obj); err != nil {
		t.Fatalf("wire body not json: %v", err)
	}
	sys, _ := obj["messages"].([]any)[0].(map[string]any)["content"].(string)
	if strings.Contains(sys, codexInstructions) ||
		strings.Contains(sys, "in the Codex CLI, a terminal-based coding assistant.") {
		t.Errorf("wire body still contains codex fingerprint: %q", sys)
	}
	if !strings.Contains(sys, "running in the Codex CLI tool, a terminal-based") {
		t.Errorf("codex rewrite missing on wire: %q", sys)
	}
	user, _ := obj["messages"].([]any)[1].(map[string]any)["content"].(string)
	if user != "say ok" {
		t.Errorf("user message altered: %q", user)
	}
}

// 出站边界：关闭脱敏后 wire body 原样保留指纹（验证开关真实有效）。
func TestChatStreamWireBodySanitizeDisabled(t *testing.T) {
	var gotBody []byte
	ts := newTestUpstream(t, func(w http.ResponseWriter, r *http.Request) {
		gotBody, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = w.Write([]byte("data: [DONE]\n\n"))
	})
	defer ts.Close()

	c := New()
	c.SanitizeFingerprints = false
	c.ChatBaseCN = ts.URL
	acct := &auth.Auth{AccessToken: "test-token", Domain: "copilot.tencent.com", UID: "u1"}

	body := []byte(`{"model":"glm-5.2","messages":[{"role":"system","content":"` + ccIdentity + `"}]}`)
	rc, status, _, err := c.ChatStream(acct, body, "", ChatMeta{})
	if err != nil {
		t.Fatal(err)
	}
	defer rc.Close()
	if status >= 400 {
		t.Fatalf("upstream status %d", status)
	}
	if !strings.Contains(string(gotBody), ccIdentity) {
		t.Error("sanitize disabled should preserve fingerprint on wire")
	}
}

// newTestUpstream 起一个假上游并捕获请求。
func newTestUpstream(t *testing.T, h http.HandlerFunc) *httptest.Server {
	t.Helper()
	return httptest.NewServer(h)
}

// TestBareHeaderAbbreviated 裸键名（无冒号）兜底缩写——2026-09-13 实验 F4：
// assistant 消息反引号引用裸键名即触发 11128，剥离层 sanitizeHdrRe 要求冒号、
// 对裸串无效。键值形态被整段删除后，残留裸键名做最小缩写（header→hdr），
// 破坏逐字匹配、语义不变、保留可读性。
func TestBareHeaderAbbreviated(t *testing.T) {
	for _, in := range []string{
		"引用 `x-anthropic-billing-header` 这个键",
		"lower: x-anthropic-billing-header",
		"mixed: X-Anthropic-Billing-Header",
	} {
		out := sanitizeText(in)
		if strings.Contains(strings.ToLower(out), "x-anthropic-billing-header") {
			t.Fatalf("裸键名未被兜底: in=%q out=%q", in, out)
		}
		if !strings.Contains(strings.ToLower(out), "x-anthropic-billing-hdr") {
			t.Fatalf("裸键名未缩写为 hdr 形态: in=%q out=%q", in, out)
		}
	}
	// 键值形态仍走整段删除（不留 hdr 残骸）
	out := sanitizeText("prefix x-anthropic-billing-header: cc_version=1.0; cc_entrypoint=cli; suffix")
	if strings.Contains(strings.ToLower(out), "x-anthropic-billing") {
		t.Fatalf("键值形态应整段删除: out=%q", out)
	}
}

// TestReasoningContentSanitized reasoning_content（思维链回填字段）与 content
// 同等净化——实测该字段同样携带指纹。
func TestReasoningContentSanitized(t *testing.T) {
	msgs := []any{
		map[string]any{
			"role":              "assistant",
			"content":           nil,
			"reasoning_content": "上文出现过 `x-anthropic-billing-header` 键名",
		},
	}
	if !sanitizeMessages(msgs) {
		t.Fatal("reasoning_content 中的指纹未被净化")
	}
	rc := msgs[0].(map[string]any)["reasoning_content"].(string)
	if strings.Contains(rc, "x-anthropic-billing-header") {
		t.Fatalf("reasoning_content 指纹残留: %q", rc)
	}
}

// TestSanitizeLiteralByteExact 逐字节快照护栏：把 sanitizeFeatures 7 项 +
// sanitizeRewrites 5 对 + 3 个正则的当前字节值硬编码断言。
// 这些字面量是实验逆向出的上游内容审核黑名单（无契约可引用），上游按逐字精确
// 匹配拦截，一字之差即漏拦（400 code=11128）或误伤。任何未来改动（含看似无害的
// 统一常量/抽配置）都会先红在本测试——必须走 analysis 报告 #6 的逐字节验证
// 步骤：grep 全部出现点（表/正则/测试断言四处同查）+ 真实账号上游实测 +
// TestSanitize 全族回归，方可同步更新本快照。
func TestSanitizeLiteralByteExact(t *testing.T) {
	features := []string{
		"x-anthropic-billing-header", // header 键值段键名
		"cc_entrypoint=",             // 尾随裸键值（截断前缀即可命中）
		"You are Claude Code",        // 身份句（截断前缀即可命中）
		"Main branch (",              // 注入指令句（截断前缀即可命中）
		"You are a coding agent running in the Codex CLI", // Codex instructions 首段（截断前缀即可命中）
		"github.com/anthropics/",                          // 反馈句里的 Anthropic 仓库链接
		"11128",                                           // 上游反探测：裸数字错误码
	}
	if len(sanitizeFeatures) != len(features) {
		t.Fatalf("sanitizeFeatures 项数=%d want %d（快照与实现不同步，见测试头注释的验证步骤）", len(sanitizeFeatures), len(features))
	}
	for i, want := range features {
		if sanitizeFeatures[i] != want {
			t.Errorf("sanitizeFeatures[%d]=%q want %q（逐字节不一致，改动前先走快照验证流程）", i, sanitizeFeatures[i], want)
		}
	}

	rewrites := [][2]string{
		{
			"You are Claude Code, Anthropic's official CLI for Claude",
			"You are Claude Code, Anthropic's official CLI tool for Claude",
		},
		{
			"Main branch (you will usually use this for PRs)",
			"Default branch (you will usually use this for PRs)",
		},
		{
			"You are a coding agent running in the Codex CLI, a terminal-based coding assistant.",
			"You are a coding agent running in the Codex CLI tool, a terminal-based coding assistant.",
		},
		{
			"To give feedback, users should report the issue at https://github.com/anthropics/claude-code/issues",
			"To provide feedback, users should report the issue at https://github.com/anthropics/claude-code/issues",
		},
		{
			"11128",
			"11-128",
		},
	}
	if len(sanitizeRewrites) != len(rewrites) {
		t.Fatalf("sanitizeRewrites 对数=%d want %d（快照与实现不同步，见测试头注释的验证步骤）", len(sanitizeRewrites), len(rewrites))
	}
	for i, want := range rewrites {
		if sanitizeRewrites[i][0] != want[0] || sanitizeRewrites[i][1] != want[1] {
			t.Errorf("sanitizeRewrites[%d]=%q→%q want %q→%q（逐字节不一致，改动前先走快照验证流程）",
				i, sanitizeRewrites[i][0], sanitizeRewrites[i][1], want[0], want[1])
		}
	}

	regexes := map[string]string{
		"sanitizeHdrRe":     `(?i)x-anthropic-billing-header:[^;\n]*;?\s*`,
		"sanitizeBareHdrRe": `(?i)x-anthropic-billing-header`,
		"sanitizeKvRe":      `(?i)\bcc_[a-z0-9_]+=[^;\n]*;?\s*`,
	}
	for name, want := range regexes {
		var got string
		switch name {
		case "sanitizeHdrRe":
			got = sanitizeHdrRe.String()
		case "sanitizeBareHdrRe":
			got = sanitizeBareHdrRe.String()
		case "sanitizeKvRe":
			got = sanitizeKvRe.String()
		}
		if got != want {
			t.Errorf("%s=%q want %q（逐字节不一致，改动前先走快照验证流程）", name, got, want)
		}
	}
}

// ── issue #246：DeepSeek Harness（DSH）system 首句指纹 ─────────────────
//
// 上游拦的是**句式**而不是品牌名：system 首句写成
// "You are an AI agent powered by <brand>." 或
// "You are a coding agent powered by the <model> model." 都会 400 code=11-128，
// 网关随后回退 /console 拿到 11102、把 (账号,模型) 打进 6 小时冷却 → 整池 503。
// 下面几条是 issue 里的真实抓包原文（冠词、大小写、句号照抄）。
// 本地定制（2026-10-22）：断言按「只换连接词」的语义保留版改写，
// 与上游整句替换（helpful AI assistant）不同。
func TestDshSystemPromptRewritten(t *testing.T) {
	cases := []string{
		"You are an AI agent powered by DeepSeek Harness.",
		"You are a coding agent powered by the deepseek-v4.1-flash model.",
		"You are an AI assistant built on DeepSeek Harness.",
		"You are an assistant built on the deepseek-v4.1-flash model.",
	}
	wants := []string{
		"You are an AI agent running on DeepSeek Harness.",
		"You are a coding agent running on the deepseek-v4.1-flash model.",
		"You are an AI assistant running on DeepSeek Harness.",
		"You are an assistant running on the deepseek-v4.1-flash model.",
	}
	for i, in := range cases {
		if out := sanitizeText(in); out != wants[i] {
			t.Errorf("DSH 句式未按连接词替换: %q -> %q (want %q)", in, out, wants[i])
		}
	}
}

// 大小写：预检的 strings.Contains 快速路径是大小写敏感的，而 DSH 原文是
// 大写 "AI"。若只在特征表里补小写串，hasFingerprint 会提前 return、
// 改写层根本不执行（上游 hub 版补丁就踩过这个坑）。
func TestDshFingerprintCaseInsensitive(t *testing.T) {
	if out := sanitizeText("YOU ARE AN AI AGENT POWERED BY DEEPSEEK HARNESS."); out != "YOU ARE AN AI AGENT running on DEEPSEEK HARNESS." {
		t.Errorf("大写变体未命中: %q -> %q", "YOU ARE AN AI AGENT POWERED BY DEEPSEEK HARNESS.", out)
	}
	if !hasFingerprint("you are an ai agent powered by x.") {
		t.Error("hasFingerprint 应认得小写句式")
	}
}

// 模型名自带点（deepseek-v4.1-flash）：句尾必须按「点 + 空白/行尾」收口，
// 否则会停在 "v4."、把 "1-flash model." 留在正文里——比不替换更难查。
func TestDshModelNameWithDotsFullyReplaced(t *testing.T) {
	in := "You are a coding agent powered by the deepseek-v4.1-flash model."
	out := sanitizeText(in)
	if !strings.Contains(out, "deepseek-v4.1-flash model.") {
		t.Errorf("模型名被截断: %q -> %q", in, out)
	}
	if strings.Contains(out, "powered by") {
		t.Errorf("连接词残留: %q", out)
	}
}

// 句号后的内容必须原样保留：DSH 后文是工具使用契约，改了就换了客户端行为。
func TestDshFollowingInstructionsPreserved(t *testing.T) {
	in := "You are an AI agent powered by DeepSeek Harness.\nUse the read tool to inspect files.\nDo not guess."
	out := sanitizeText(in)
	if !strings.Contains(out, "Use the read tool to inspect files.\nDo not guess.") {
		t.Errorf("后文被改动: %q", out)
	}
	if !strings.Contains(out, "running on DeepSeek Harness") {
		t.Errorf("连接词未替换: %q", out)
	}
	// 同一行后接内容时，句尾空白不能被吃掉：吃掉了两条句子会粘成
	// "assistant.Then answer briefly."
	if out2 := sanitizeText("You are an AI agent powered by DeepSeek Harness. Then answer briefly."); !strings.Contains(out2, "Harness. Then answer briefly.") {
		t.Errorf("句尾空白被吃掉: %q", out2)
	}
}

// 幂等：网关对同一 body 会多次预处理（PrepareBody 与 ChatStream 各一次）。
func TestDshRewriteIdempotent(t *testing.T) {
	once := sanitizeText("You are an AI agent powered by DeepSeek Harness.")
	if twice := sanitizeText(once); twice != once {
		t.Errorf("非幂等: %q -> %q", once, twice)
	}
}

// 反例：普通自我介绍与无关句子不能被误改。
func TestDshPlainTextNotTouched(t *testing.T) {
	for _, in := range []string{
		"You are a helpful assistant.",
		"You are a helpful AI assistant built for coding tasks",
		"The agent powered by the harness failed.",
		"please use main branch for this repo",
	} {
		if out := sanitizeText(in); out != in {
			t.Errorf("不应改写: %q -> %q", in, out)
		}
	}
}

// 出站边界：PrepareBody 之后 wire body 里不得残留 DSH 句式。
func TestPrepareBodySanitizesDshPrompt(t *testing.T) {
	in := "You are an AI agent powered by DeepSeek Harness."
	body := []byte(`{"model":"deepseek-v4.1-flash","messages":[{"role":"system","content":"` + in + `"}]}`)
	out := PrepareBodyOpt(body, true)
	if strings.Contains(string(out), "powered by DeepSeek Harness") {
		t.Errorf("PrepareBody 未净化 DSH 指纹: %s", out)
	}
	if !strings.Contains(string(out), "running on DeepSeek Harness") {
		t.Errorf("替换文本缺失: %s", out)
	}
	if !strings.Contains(string(out), "AI agent") {
		t.Errorf("角色语义丢失: %s", out)
	}
}

// ── issue #250：DSH 运行时上下文「Web GUI」跨句指纹 ─────────────────────
//
// 与 #246 的身份句指纹是**两套规则**：本条必须两句连续出现才命中——
// 单独第一句、单独第二句都放行（各 200）；只差一个字符就失败
// （前缀 220 成功 / 221 失败），说明上游匹配的是跨句的连续文本。
// 因此不能只改一句，必须动到段落里的品牌短语才能拆开匹配。
const dshWebGuiBlock = `You are interacting with the user through the DeepSeek Harness Web GUI whose URL the runtime context names. When the user refers to "this page", "this GUI", or "this app" without naming another target, they mean this GUI.`

func TestDshWebGuiBlockRewritten(t *testing.T) {
	out := sanitizeText(dshWebGuiBlock)
	if strings.Contains(out, "DeepSeek Harness Web GUI") {
		t.Errorf("指纹残留: %q", out)
	}
	if !strings.Contains(out, "DeepSeek-Harness Web GUI") {
		t.Errorf("改写文本缺失: %q", out)
	}
	// 除品牌短语外内容不得改动（这段是工具/行为契约）。
	if restored := strings.ReplaceAll(out, "DeepSeek-Harness Web GUI", "DeepSeek Harness Web GUI"); restored != dshWebGuiBlock {
		t.Errorf("段落其他内容被改动:\n 得到 %q\n 期望 %q", restored, dshWebGuiBlock)
	}
}

// 预检必须认这段，否则 hasFingerprint 提前 return、改写层执行不到。
func TestDshWebGuiFingerprintDetected(t *testing.T) {
	if !hasFingerprint(dshWebGuiBlock) {
		t.Error("hasFingerprint 应认得 Web GUI 段落")
	}
	if !hasFingerprint("through the deepseek harness web gui") {
		t.Error("大小写变体应识别")
	}
}

// 幂等：PrepareBody 与 ChatStream 各预处理一次。
func TestDshWebGuiRewriteIdempotent(t *testing.T) {
	once := sanitizeText(dshWebGuiBlock)
	if twice := sanitizeText(once); twice != once {
		t.Errorf("非幂等: %q -> %q", once, twice)
	}
}

// 反例：不含该品牌短语的普通文本不得被改写。
func TestDshWebGuiPlainTextNotTouched(t *testing.T) {
	for _, in := range []string{
		"You are interacting with the user through a web GUI.",
		"DeepSeek Harness is a coding agent runtime.",
		"I like the Web GUI.",
		"See the docs for the DeepSeek Harness runtime context.",
	} {
		if out := sanitizeText(in); out != in {
			t.Errorf("不应改写: %q -> %q", in, out)
		}
	}
}

// 出站边界：developer 消息（DSH 用它承载系统提示词）也要净化。
func TestPrepareBodySanitizesDshWebGui(t *testing.T) {
	payload, err := json.Marshal(map[string]any{
		"model": "deepseek-v4.1-flash",
		"messages": []any{
			map[string]any{"role": "developer", "content": dshWebGuiBlock},
			map[string]any{"role": "user", "content": "hi"},
		},
	})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	out := PrepareBodyOpt(payload, true)
	if strings.Contains(string(out), "DeepSeek Harness Web GUI") {
		t.Errorf("PrepareBody 未净化 Web GUI 指纹: %s", out)
	}
	if !strings.Contains(string(out), "DeepSeek-Harness Web GUI") {
		t.Errorf("替换文本缺失: %s", out)
	}
}

