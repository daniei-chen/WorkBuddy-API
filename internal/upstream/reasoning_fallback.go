package upstream

import (
	"sync/atomic"
)

// reasoningAsContent 是「思考通道兜底」的进程级开关（issue #135）。
//
// 背景：国际版 deepseek 系模型在思考模式下，答案只出现在 reasoning_content，
// content 恒为空。只看 content 的 OpenAI 兼容客户端（大多数）会把「只有思考」
// 判成空回复，用户看到的就是「模型没有输出」。
//
// 兜底条件刻意很窄：**整轮既没有正文、也没有工具调用**，才把思考原文同时作为
// content 下发。有正文或有工具调用的正常轮次零改动。
//
// 已知代价：同时渲染 reasoning_content 与 content 的客户端（如 DeepSeek 自家
// 客户端）会把同一段话显示两遍。默认开启（空回复比重复更难接受），需要时可用
// 配置 `reasoning_as_content: false` 或环境变量 `WB2A_REASONING_AS_CONTENT=0` 关掉。
//
// 用 atomic 而不是普通变量：读发生在每个请求的流处理 goroutine 里，测试会并发
// 跑；启动后只写一次，读多写一，atomic 足够且免锁。
var reasoningAsContent atomic.Bool

func init() {
	reasoningAsContent.Store(true)
}

// ConfigureReasoningFallback 设置兜底开关（启动时调用一次；测试可来回切）。
func ConfigureReasoningFallback(on bool) {
	reasoningAsContent.Store(on)
}

// ReasoningFallbackEnabled 报告当前是否启用兜底（供测试与启动日志使用）。
func ReasoningFallbackEnabled() bool {
	return reasoningAsContent.Load()
}
