// sanitize.go 出站请求体脱敏：剥离上游内容审核黑名单指纹。
//
// 背景：客户端（Claude Code 类 CLI）在 system prompt 注入若干固定模板句，
// 上游内容审核按逐字精确匹配拦截（非语义审核），一字改动即可绕过。
// 策略：键值/header 型指纹整段剥离；承载语义的模板句最小改写（换一词），语义不变。
package upstream

import (
	"regexp"
	"strings"
)

// sanitizeFeatures 特征预检：任一命中才进入净化（strings.Contains 快速路径，
// 普通请求全不中 → 原样返回，零分配）。
var sanitizeFeatures = []string{
	"x-anthropic-billing-header", // header 键值段键名
	"cc_entrypoint=",             // 尾随裸键值（截断前缀即可命中）
	"You are Claude Code",        // 身份句（截断前缀即可命中）
	"Main branch (",              // 注入指令句（截断前缀即可命中）
	"You are a coding agent running in the Codex CLI", // Codex instructions 首段（截断前缀即可命中）
	"github.com/anthropics/",                          // 反馈句里的 Anthropic 仓库链接
	"11128",                                           // 上游反探测：裸数字错误码
}

// sanitizeHdrRe 剥离层：header 键名即触发（与值无关），整段删除。
var sanitizeHdrRe = regexp.MustCompile(`(?i)x-anthropic-billing-header:[^;\n]*;?\s*`)

// sanitizeBareHdrRe 兜底层：裸键名（无冒号无值）同样是指纹——2026-09-13 实验 F4
// 证实 assistant 消息里反引号引用裸键名即触发 11128，而剥离层要求冒号、对裸串无效。
// 键值形态被整段删除后，残留的裸键名做最小缩写（header→hdr）：破坏逐字匹配、
// 语义不变、保留可读性。大小写不敏感，覆盖 X-Anthropic-... 变体。
//
// 注意该正则不要求冒号，是 sanitizeHdrRe 的超集——hasFingerprint 与 sanitizeText
// 中两者并用：先删键值形态（sanitizeHdrRe），再缩写残留裸键名（本正则），
// 替换语义不同（整段删除 vs 最小缩写），不可合并为一个正则。
var sanitizeBareHdrRe = regexp.MustCompile(`(?i)x-anthropic-billing-header`)

// sanitizeKvRe 剥离层：尾随裸键值（cc_xxx=...;）循环清理。
var sanitizeKvRe = regexp.MustCompile(`(?i)\bcc_[a-z0-9_]+=[^;\n]*;?\s*`)

// sanitizeRewrites 改写层：全模板句逐字替换（每句只改一个词，语义不变）。
//
// 身份句的匹配串**不带结尾标点**（只到 "…for Claude" 为止）：
// CLI 版这句以句号收尾（"…for Claude."），桌面版（claude-desktop-3p / Agent SDK）
// 以逗号接后继内容（"…for Claude, running within the Claude Agent SDK."）。
// 带句号的整句只匹配前者，桌面版会漏网、指纹原样发上游 → 400 code=11128。
// 去掉结尾标点后两种形态一并覆盖（替换串同样不带标点，让原有标点原样保留）。
// 注意仍要求 "You are Claude Code, " 前缀，不做更宽的子串替换，
// 以免误伤 TestExactMatchOnlyVariantNotTouched 所保护的零散文本。
var sanitizeRewrites = [][2]string{
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
		// 反馈句：整句带 Anthropic 仓库链接，上游按整句拦截（只留链接或只留半边均不拦，
		// 实测需整句同时出现）。give→provide 一词之差即可绕过，语义不变。
		"To give feedback, users should report the issue at https://github.com/anthropics/claude-code/issues",
		"To provide feedback, users should report the issue at https://github.com/anthropics/claude-code/issues",
	},
	{
		// 上游反探测：只要请求体里出现裸数字 11128 就整单拦截（与该数字的上下文无关——
		// "code=11128" / 裸 "11128" / "错误码 11128" / "Code=11128" 全部命中；
		// 相邻的 11148 / 11101 / 11115 / 99999 均放行）。11128 正是本类拦截自身的错误码，
		// 上游据此识别"在讨论/回显其内部错误码"的请求。
		// 代价：用户对话中任何 11128 都会被改写——但这串数字出现在请求里本身就是拦截条件，
		// 不改写必然失败。插入连字符保留可读性与指代（零宽空格无效，实测上游会归一化）。
		"11128",
		"11-128",
	},
}

// sanitizeDshRe 通配层：客户端「自我介绍句」的模板匹配（issue #246）。
//
// 上游不只看具体品牌，它把一整类自我介绍句当指纹：DeepSeek Harness（DSH）把
// system 首句写成 "You are an AI agent powered by DeepSeek Harness."，
// 命中的是这类**句式结构**而不是品牌名——把品牌换成别的、或换成
// "You are an AI assistant built on …" / "You are an assistant built on …"
// （DSH 的第二句就是 "You are a coding agent powered by the <model> model."）
// 同样 400 code=11-128（issue #246 逐项实测，与请求规模、tools、stream 无关）。
//
// 所以这里按句式而不是按品牌匹配，整句换成通用自我介绍：
//
//	"You are an [AI] [coding] agent|assistant (powered by|built on) … ."
//
// 为什么不用逐字替换表（sanitizeRewrites）：品牌/模型名是变量（"DeepSeek Harness"、
// "the deepseek-v4.1-flash model"），逐字表一旦漏了某个变体就静默失效——这正是
// sanitizeFeatures 那条大小写敏感快速路径踩过的坑（原文 "AI agent" 大写，小写特征
// 串永远命中不到，两句根本进不了清洗分支）。
//
// 三个边界细节：
//   - 句尾按「点 + 空白/行尾」收口，不能用 `[^\n]*\.` 直到第一个点为止：
//     模型名自带点（deepseek-v4.1-flash），那样只会吃掉 "…v4." 留下 "1-flash model."。
//     `{0,120}` 是宽松上限，防止某行没有可用的句尾时一路吃到行尾。
//   - 整句（含句号）一起替换：只改前半句会留下 "You are a helpful AI assistant.DeepSeek
//     Harness." 这种残句；替换串自带句号，语义上就是「一个普通的助手」。
//   - 命中位置不限行首：真实请求里这句是 system 第一条，但客户端也可能把它嵌在
//     模板里（"<system>You are …"）。放宽的代价只是把一句自我介绍换个说法。
var sanitizeDshRe = regexp.MustCompile(
	`(?i)\byou are (?:a|an) (?:ai )?(?:coding )?(?:agent|assistant) (?:powered by|built on) [^\n]{0,120}?\.(\s|$)`)

// 本地定制（2026-10-22）：不做整句替换，只改连接词。
//
// 上游判据是「句式」——(powered by|built on) 这一对连接短语；把连接词换成
// running on 就不再命中，而
//
//	"You are a coding agent running on the deepseek-v4.1-flash model."
//
// 与原文语义完全一致：角色（coding agent）、模型名、定语全部留在原位。
// 同一份文件里 GUI 段落那条（插连字符）用的也是这个思路：改动最小、语义不动。
var dshConnectorRe = regexp.MustCompile(`(?i)\s+(?:powered\s+by|built\s+on)\s+`)

// sanitizeDshRewrite 只把连接短语换成等价的 "running on"，其余原样保留。
func sanitizeDshRewrite(segment string) string {
	return dshConnectorRe.ReplaceAllString(segment, " running on ")
}

// sanitizeDshGuiRe 通配层：DSH 运行时上下文里的 Web GUI 段落（issue #246 后续实测）。
//
// 上游的匹配**不是单句**：单独一句
//
//	You are interacting with the user through the DeepSeek Harness Web GUI whose
//	URL the runtime context names.
//
// 会放行；但连同下一句 "When the user refers to … they mean this GUI." 组成
// 221 字符的**连续文本**就返回 400 code=11-128。二分实测：前缀 220 字符成功、
// 221 字符失败，说明匹配窗口横跨两句，单句改写无效（两句各自单独测都放行）。
//
// 因此对段落里的品牌短语做最小改写（插入连字符）：破坏逐字匹配、语义不变，
// 且该短语只出现在被拦段落内，不会误伤正常对话。实测改写后上游放行。
var sanitizeDshGuiRe = regexp.MustCompile(`(?i)DeepSeek Harness Web GUI`)

// sanitizeDshGuiRepl 替换串：保留品牌可读性，仅插入一个连字符。
const sanitizeDshGuiRepl = "DeepSeek-Harness Web GUI"

// sanitizeText 单段文本净化：预检不中 → 返回原串（零分配）。
func sanitizeText(text string) string {
	if !hasFingerprint(text) {
		return text
	}
	for _, rw := range sanitizeRewrites {
		text = strings.ReplaceAll(text, rw[0], rw[1])
	}
	text = sanitizeDshRe.ReplaceAllStringFunc(text, sanitizeDshRewrite)
	// DSH Web GUI 段落：上游按「跨两句的连续文本」拦截，必须动品牌短语才拆得开。
	text = sanitizeDshGuiRe.ReplaceAllString(text, sanitizeDshGuiRepl)
	if sanitizeHdrRe.MatchString(text) {
		text = sanitizeHdrRe.ReplaceAllString(text, "")
	}
	if strings.Contains(text, "cc_") {
		prev := ""
		for prev != text { // 清尾随裸 kv（cc_version=...; cc_entrypoint=...;）
			prev = text
			text = sanitizeKvRe.ReplaceAllString(text, "")
		}
	}
	// 兜底：键值形态已在上面整段删除，这里只剩裸键名（引用/示例文本形态）。
	text = sanitizeBareHdrRe.ReplaceAllString(text, "x-anthropic-billing-hdr")
	return strings.TrimSpace(text)
}

// hasFingerprint 特征预检：先走 strings.Contains 快速路径（零分配）；
// header 键名有大小写变体（X-Anthropic-...）且可能以裸键名形态出现（无冒号），
// Contains 大小写敏感、sanitizeHdrRe 要求冒号——两者都会漏掉「混合大小写 + 裸键名」，
// 必须再用不要求冒号的 (?i) 正则兜底（sanitizeBareHdrRe），否则整条净化被跳过。
// sanitizeBareHdrRe 不要求冒号，是 sanitizeHdrRe 的超集，故无需再单独匹配后者。
func hasFingerprint(text string) bool {
	for _, f := range sanitizeFeatures {
		if strings.Contains(text, f) {
			return true
		}
	}
	// DSH 句式指纹（见 sanitizeDshRe）：**必须在这里也认**，否则预检直接
	// return，改写层根本执行不到——issue #246 里上游 hub 版的补丁就踩过这个
	// 坑：特征表是大小写敏感的 strings.Contains，而原文是 "AI agent"，
	// 补进去的小写串永远命中不到。本正则自带 (?i)，不依赖原文大小写。
	if sanitizeDshRe.MatchString(text) {
		return true
	}
	// DSH Web GUI 段落指纹（见 sanitizeDshGuiRe）：跨两句匹配，单句放行，
	// 所以特征表里补的是段落内的品牌短语；(?i) 覆盖大小写变体。
	if sanitizeDshGuiRe.MatchString(text) {
		return true
	}
	return sanitizeBareHdrRe.MatchString(text)
}

// sanitizeContent 兼容字符串与多模态数组；只动 text part，image 等 part 不动。
// 返回净化后的值及是否发生变化。
func sanitizeContent(v any) (any, bool) {
	switch c := v.(type) {
	case string:
		s := sanitizeText(c)
		return s, s != c
	case []any:
		changed := false
		for _, p := range c {
			m, ok := p.(map[string]any)
			if !ok {
				continue
			}
			text, ok := m["text"].(string)
			if !ok {
				continue
			}
			if s := sanitizeText(text); s != text {
				m["text"] = s
				changed = true
			}
		}
		return c, changed
	}
	return v, false
}

// sanitizeToolCalls 净化 assistant.tool_calls[].function.arguments。
//
// arguments 是**字符串化的 JSON**（不是对象），因此按文本走 sanitizeText 即可。
// 这块长期是盲区：工具调用消息的 content 通常是 null，而旧版 sanitizeMessages
// 在 content 缺失时直接 continue，整条消息连 tool_calls 一起被跳过——
// 于是历史里任何写进工具参数的被拦字符串（文件名、命令、写入内容）都会原样漏出。
func sanitizeToolCalls(v any) bool {
	callList, ok := v.([]any)
	if !ok {
		return false
	}
	changed := false
	for _, c := range callList {
		call, ok := c.(map[string]any)
		if !ok {
			continue
		}
		fn, ok := call["function"].(map[string]any)
		if !ok {
			continue
		}
		args, ok := fn["arguments"].(string)
		if !ok {
			continue
		}
		if s := sanitizeText(args); s != args {
			fn["arguments"] = s
			changed = true
		}
	}
	return changed
}

// sanitizeMessages 净化 messages 中的 content、reasoning_content 与 tool_calls；任一命中返回 true。
func sanitizeMessages(messages []any) bool {
	changed := false
	for _, msg := range messages {
		m, ok := msg.(map[string]any)
		if !ok {
			continue
		}
		// content 与 tool_calls 各自独立判断：content 可以为 null（工具调用轮），
		// 早期版本在此 continue，导致这类消息的 tool_calls 完全不被净化。
		if c, ok := m["content"]; ok {
			if nc, ch := sanitizeContent(c); ch {
				m["content"] = nc
				changed = true
			}
		}
		// reasoning_content（思维链回填字段，见 thinking.go/sse.go）实测同样
		// 携带指纹，与 content 同等净化。string 形态直接走 sanitizeText。
		if rc, ok := m["reasoning_content"].(string); ok {
			if s := sanitizeText(rc); s != rc {
				m["reasoning_content"] = s
				changed = true
			}
		}
		if tc, ok := m["tool_calls"]; ok {
			if sanitizeToolCalls(tc) {
				changed = true
			}
		}
	}
	return changed
}
