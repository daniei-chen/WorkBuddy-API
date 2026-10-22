package upstream

import "testing"

// CodeOf 从上游响应体里取业务码（issue #201 缺陷 1）。
//
// 为什么需要它：禁用/冷却的理由里此前**写死**了 11140，而现场真实响应是
// `http 400 + code 11-128` —— 排障的人拿着 11140 去搜会得到完全错误的方向
// （11140 指向"UA 平台段送错"，真实原因是渠道校验）。理由里带响应自己的码，
// 才不会把人带偏。
func TestCodeOf(t *testing.T) {
	cases := []struct {
		name string
		body string
		want string
	}{
		{"数字形态", `{"error":{"data":{"code":11140,"msg":"request illegal"}}}`, "11140"},
		{"字符串形态", `{"code":"11148","msg":"tool calls and tool results do not match"}`, "11148"},
		{"带横线的字符串形态", `{"code":"11-128","msg":"conversation id missing"}`, "11-128"},
		// 上游偶尔给出**非法 JSON**：code 的值没加引号（issue #201 的原始响应就长这样）。
		// 用 json.Unmarshal 会整条解析失败 → 取不到码，所以这里必须是正则容忍。
		{"裸值 11-128（非法 JSON）", `{"code":11-128,"msg":"conversation id missing","requestId":"x"}`, "11-128"},
		{"带空格", `{ "code" : 14018 , "msg" : "no credit" }`, "14018"},
		{"没有 code 字段", `{"msg":"boom"}`, ""},
		{"空体", ``, ""},
		{"HTML 拦截页", `<html><body>403</body></html>`, ""},
		// requestId 里出现数字不该被当成业务码（只认 "code" 键）
		{"只有 requestId", `{"requestId":"11140-abc","msg":"x"}`, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := CodeOf(tc.body); got != tc.want {
				t.Errorf("CodeOf(%q)=%q want %q", tc.body, got, tc.want)
			}
		})
	}
}

// 11-128「conversation id missing」必须归入账号级故障（issue #193/#201 缺陷 2）。
//
// 它此前落通用 4xx → "只换号不罚号"，坏号留在池里"显示健康、每次被选中都失败"
// （报告里 14 个号中 7 个被拦死、却只有 2 个被禁用）。归到 ErrAccountFault 之后由
// applyErrorPolicy 按账号级处理（软冷却，不禁用）。
func TestClassifyConversationIDMissingIsAccountFault(t *testing.T) {
	bodies := []string{
		`{"code":"11-128","msg":"conversation id missing"}`,
		`{"code":11-128,"msg":"conversation id missing"}`, // 非法 JSON 形态
		`{"code":11-128,"msg":"Conversation ID Missing"}`, // 大小写不敏感
	}
	for _, body := range bodies {
		if got := Classify(400, body); got != ErrAccountFault {
			t.Errorf("Classify(400, %q)=%v want ErrAccountFault", body, got)
		}
	}
	// 不带该文案的 11-128（其它渠道校验原因）仍走原分类，不被这条规则吞掉。
	if got := Classify(400, `{"code":"11-128","msg":"请求被拦截，请重新发送"}`); got == ErrAccountFault {
		t.Error("只有 conversation id missing 该归账号级；其它 11-128 文案不该被吞进账号级分类")
	}
}
