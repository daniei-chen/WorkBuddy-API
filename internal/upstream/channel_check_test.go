package upstream

import "testing"

// 渠道校验的识别与路径兜底（2026-10-17 用户报告）。
//
// 现场：上游对 /v2/chat/completions 新增渠道强校验，被校验到的请求回
// 「没通过渠道校验，可能不是官方客户端发出的」；而官方客户端走的 /console 端点放行。
// 此前这类响应要么落通用 4xx（只换号不罚、坏号留在池里次次失败），要么被封套里的
// `request illegal` 带进「账号被封 → 硬禁用」—— 于是面板上显示「已禁用（需重新登录）」，
// 用户重扫也没用（凭证从来没问题）。
//
// 注意：下面这串业务码**一律拼接构造**，不手写整串 —— 它在手抄时极易写成 1138/1128，
// 而它正是判据本身，抄错会让测试「通过」一个不存在的判据（第一次写这文件就抄错了）。
var (
	codeDashed = "1" + "1" + "-" + "1" + "2" + "8" // 实测体里的写法
	codePlain  = "1" + "1" + "1" + "2" + "8"       // 报告方另见的写法
)

// channelBody 一次渠道校验的真实封套：中文「渠道校验」+ 英文 displayTips +
// 历史遗留的 `request illegal` msg（后者正是误禁用账号的触发点）。
var channelBody = `{"code":` + codePlain + `,"msg":"request illegal","requestId":"x",` +
	`"displayMsg":{"zh":"请求被拦截，请重新发送"},` +
	`"displayTips":{"zh":"这次调用没通过渠道校验，可能不是官方客户端发出的。请用官方版本重新发送。",` +
	`"en":"This call failed channel verification, possibly because it was not sent from an official client."}}`

func TestIsChannelCheckMatches(t *testing.T) {
	if !IsChannelCheck(channelBody) {
		t.Fatal("中文「渠道校验」文案必须被识别")
	}
	for _, code := range []string{
		`"code":` + codeDashed,
		`"code":` + codePlain,
		`"code":"` + codeDashed + `"`,
	} {
		body := `{` + code + `,"displayTips":{"en":"This call failed channel verification"}}`
		if !IsChannelCheck(body) {
			t.Errorf("码写法 %s 没被识别", code)
		}
	}
	// 英文 displayTips 单独也要能命中（中文可能被上游换掉）。
	if !IsChannelCheck(`{"code":` + codePlain + `,"displayTips":{"en":"possibly because it was not sent from an official client"}}`) {
		t.Error("英文 official client 文案没被识别")
	}
}

func TestIsChannelCheckDoesNotSwallowContentBlock(t *testing.T) {
	// 同一个码也承载「内容审核拦截」：那种必须保持原行为（不换路径、不罚号、
	// 走降级重试），不能被这条规则吞成账号级。
	content := `{"code":` + codeDashed + `,"msg":"blocked by security policy"}`
	if IsChannelCheck(content) {
		t.Fatal("内容审核拦截被误判成渠道校验 —— 会让它不再走降级重试")
	}
	if got := Classify(400, content); got != ErrContentBlocked {
		t.Errorf("内容审核拦截的分类被影响：%v", got)
	}
	if IsChannelCheck(`{"msg":"request illegal"}`) {
		t.Error("没有渠道校验文案时不该命中")
	}
}

func TestClassifyRoutesChannelCheckToAccountFault(t *testing.T) {
	// 归账号级是为了让 handler 的 ErrAccountFault 分支接管（那里最先判它、只软冷却）。
	// 落通用 4xx 的后果是「只换号不罚」：坏号留在池里、每次被选中都失败。
	if got := Classify(400, channelBody); got != ErrAccountFault {
		t.Errorf("渠道校验应归 ErrAccountFault（供 handler 软冷却），got %v", got)
	}
}

func TestChatPathsHaveConsoleFallback(t *testing.T) {
	c := &Client{}
	paths := c.chatPaths(nil)
	if len(paths) != 2 || paths[0] != "/v2/chat/completions" || paths[1] != "/console/chat/completions" {
		t.Fatalf("路径候选应为 [/v2, /console]（/v2 优先，兜底 /console），got %v", paths)
	}
}
