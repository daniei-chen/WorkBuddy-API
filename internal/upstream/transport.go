// transport.go 出站 Transport 构造的单一事实来源（连接层加固，吸收 kongjianguan
// 4 连击实测经验的前三件，见 .claude/reports/fork-scan-absorb.md T-2）：
// 真正禁 h2 / TLS 握手超时 / 短 keepalive 探测，全参数集中定义可测试可调整。
// 第四件 DisableKeepAlives 按报告 trade-off 不吸收（每请求 TLS 握手开销与连接
// 复用意图相反），分析见 .claude/reports/transport-hardening.md。
package upstream

import (
	"crypto/tls"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"
)

// 连接层参数集中定义（与 server/backoff.go 同风格：一处定义，测试可回读断言）。
const (
	// dialTimeout TCP 连接建立上限。半死连接的第一道闸：连不上就快速失败
	// 轮转换号，不再干等系统 TCP 重传窗口（kongjianguan 实测半开 TCP 单次
	// TTFB 卡 936s——默认 Dialer 无超时上限）。
	dialTimeout = 10 * time.Second
	// dialKeepAlive TCP keepalive 探测周期。默认 Dialer 2h 才发首个探测——
	// NAT 黑洞里 2h 足够连接半死且被复用。15s 周期让死连接在 15~30s 内被
	// 内核掐掉（RST/ETIMEDOUT），复用侧立即感知而非卡到重传窗口。
	dialKeepAlive = 15 * time.Second
	// tlsHandshakeTimeout TLS 握手上限。现役此前完全缺失——握手挂起时无任何
	// 层兜底（ResponseHeaderTimeout 只在请求写完后才计时），只能干等到
	// HTTP.Client.Timeout(120s)。
	tlsHandshakeTimeout = 10 * time.Second
	// idleConnTimeout 空闲连接池保留时长。从 90s 收到 30s：WAF 风暴后上游
	// NGI 常态性掐闲置连接，90s 池里的连接多半已死（kongjianguan 同款取值）；
	// 复用侧仍有 15s keepalive 兜底识别。
	idleConnTimeout = 15 * time.Second
	// responseHeaderTimeout 聊天 SSE 首字节前（响应头）硬上限。从 120s 收到
	// 60s：kongjianguan 笔记实录成功请求 TTFB 曾到 16s（慢模型冷启动），
	// 60s ≈ 3.75× 观测最坏健康首包，留足慢冷启动余量；同时把半死连接场景的
	// 单请求卡死从 2 分钟压到 1 分钟（MaxRotate 默认 3 次的最坏轮转从 6 分钟
	// 压到 3 分钟）。不取 kongjianguan 的 20s：其 20s 是 DisableKeepAlives+
	// timedConn 20s 写超时组合的取值，我们保留连接复用，须按自身慢冷启动
	// 观测留余量。语义核对（任务书设计纪律）：ResponseHeaderTimeout 只计响应
	// 头到达前的时长，头到达后 SSE 长流不受影响（流中空闲由 IdleTimeout 监控，
	// 见 idle.go），不误杀长流——transport_test.go 有显式回归。
	//
	// 注意：本常量只是 newTransport 的构造默认，main.go 会按 config
	// upstream.header_timeout_seconds 无条件覆盖。因此生产生效值 = config
	// 解析值（未配置时 normalize 回落 timeout_seconds，默认 120），本 60s 仅作
	// 「Config 未接线/测试裸用」时的安全网——与 config.example.json 的取值
	// 对齐避免三处口径漂移（transport 60 / config 回落 120 / example 60）。
	responseHeaderTimeout = 60 * time.Second
)

// maxIdleConns / maxIdleConnsPerHost 连接池容量（既有值，一并集中定义）。
const (
	maxIdleConns        = 100
	maxIdleConnsPerHost = 20
)

// newDialer 构造出站拨号器（DialContext 的 Timeout/KeepAlive 参数集中于此，
// 供测试回读断言）。
func newDialer() *net.Dialer {
	return &net.Dialer{
		Timeout:   dialTimeout,
		KeepAlive: dialKeepAlive,
	}
}

// newTransport 构造共享出站 Transport（HTTP 与 ChatHTTP 同一实例，连接池不重复）。
// 分两层防半死连接：
//   - TLS 层：空 TLSNextProto 真正禁 h2（kongjianguan 二次修正的实证：ForceAttemptHTTP2=false
//     只对自定义 Dial 生效，默认 TLS 经 ALPN 仍协商出 h2，半死 h2 流复用表现为
//     "http2: timeout awaiting response headers"——唯一正确写法是置空映射，让 ALPN
//     完成后无 h2 协议可用，连接退回 HTTP/1.1）。
//   - TCP 层：DialContext 10s 建连上限 + 15s keepalive 探测，半开连接在建立期
//     和复用期都能被快速识别（见 dialTimeout/dialKeepAlive 注释）。
func newTransport() *http.Transport {
	dialer := newDialer()
	return &http.Transport{
		DialContext: dialer.DialContext,
		// 出口代理：走标准环境变量（HTTP_PROXY / HTTPS_PROXY / NO_PROXY，大小写都认）。
		//
		// 自定义 Transport **不设 Proxy 字段即为 nil**，这与 http.DefaultTransport
		// 默认读环境变量不同 —— 于是「容器里配好代理、curl 能通、网关却仍走直连」
		// 成了一种没有任何日志的静默失败（issue #136，群晖 + 代理出网的部署）。
		// ProxyFromEnvironment 同时负责 NO_PROXY：内网/回环地址按环境变量绕过。
		Proxy: http.ProxyFromEnvironment,
		// 空 TLSNextProto（非 nil）真正禁 h2：见函数注释。必须 make 而非 nil——
		// nil 表示「让标准库注入默认 h2 映射」（kongjianguan 实测：设
		// ForceAttemptHTTP2=false 后日志仍报 h2 timeout，正是这个陷阱）。
		TLSNextProto:          make(map[string]func(authority string, c *tls.Conn) http.RoundTripper),
		TLSHandshakeTimeout:   tlsHandshakeTimeout,
		MaxIdleConns:          maxIdleConns,
		MaxIdleConnsPerHost:   maxIdleConnsPerHost,
		IdleConnTimeout:       idleConnTimeout,
		ResponseHeaderTimeout: responseHeaderTimeout,
	}
}

// DirectRouteName 是账号级「强制直连」的保留线路名（issue #223）。
//
// 账号文件里的 proxy 为空表示「默认出口」= 可能走进程级环境代理；要表达
// 「这个号绝不走代理」，需要显式写入 __direct__。保留名不能同时在 config.json
// 的 proxies 里使用（ConfigureProxies 会忽略它），避免语义歧义。
const DirectRouteName = "__direct__"

var directTransport = sync.OnceValue(func() *http.Transport {
	tr := newTransport()
	// newTransport 默认跟随 HTTP(S)_PROXY；直连必须显式清空。空闲连接池与
	// 其它线路分开（每个 Transport 自带池），不会把代理连接复用给直连。
	tr.Proxy = nil
	return tr
})

// ── 账号级出口线路（issue #210）──────────────────────────────────────────
//
// 面板早就能给账号绑一条命名线路（`auths/*.json` 顶层的 `proxy` 字段），但上游
// 此前**完全不读**它：出站只有 `ProxyFromEnvironment`（进程级），于是所有账号共用
// 宿主出口 IP —— 界面上看起来已经隔离，实际没有。多号同 IP 正是平台最容易关联的
// 特征，所以这里按线路名构造并**分开缓存** Transport（连接池必须分开：共用一份会把
// A 线路的空闲连接复用给 B 线路，出口照样会串）。
//
// 线路表来自 `config.json` 的 `proxies` 段（线路名 → 代理地址）。线路名查不到时
// **明确报错、不静默直连** —— 静默直连会给出「以为隔离了其实没隔离」这个最坏结果。
var routes = struct {
	mu    sync.RWMutex
	table map[string]string
	cache map[string]*http.Transport
}{table: map[string]string{}, cache: map[string]*http.Transport{}}

// ConfigureProxies 注入「线路名 → 代理地址」表（main 启动时调用一次；传 nil 清空）。
func ConfigureProxies(table map[string]string) {
	routes.mu.Lock()
	defer routes.mu.Unlock()
	routes.table = map[string]string{}
	// 清缓存：地址改了必须重建连接池，否则旧代理连接会继续用
	routes.cache = map[string]*http.Transport{}
	for name, addr := range table {
		n := strings.TrimSpace(name)
		v := strings.TrimSpace(addr)
		// 保留名由 TransportFor 直接处理，不能出现在线路表里（否则一个名为
		// __direct__ 的代理线路会被静默忽略，排障时找不到原因）。
		if n != "" && n != DirectRouteName && v != "" {
			routes.table[n] = v
		}
	}
}

// RouteNames 已配置的线路名（升序；供启动日志与排障）。
func RouteNames() []string {
	routes.mu.RLock()
	defer routes.mu.RUnlock()
	out := make([]string, 0, len(routes.table))
	for name := range routes.table {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

// normalizeProxyURL 归一化代理地址：`socks5h://` 与 `socks5://` 等价（Go 的 socks5
// 拨号本身把域名交给代理解析），统一成 `socks5://`；其余 scheme 原样。
func normalizeProxyURL(raw string) (*url.URL, error) {
	v := strings.TrimSpace(raw)
	if v == "" {
		return nil, fmt.Errorf("empty proxy address")
	}
	u, err := url.Parse(v)
	if err != nil {
		return nil, fmt.Errorf("invalid proxy address: %w", err)
	}
	switch strings.ToLower(u.Scheme) {
	case "socks5h":
		u.Scheme = "socks5"
	case "socks5", "http", "https":
	default:
		return nil, fmt.Errorf("unsupported proxy scheme %q (want http/https/socks5/socks5h)", u.Scheme)
	}
	if u.Host == "" {
		return nil, fmt.Errorf("proxy address has no host: %q", v)
	}
	return u, nil
}

// TransportFor 返回线路名对应的出站 Transport：
//   - 空名 → 默认（进程级环境代理，见 newTransport）
//   - 已配置 → 该线路专属 Transport（按名缓存，连接池与默认分开）
//   - 未配置 → error（不静默直连；错误里带线路名，便于对照 config.json）
func TransportFor(name string) (*http.Transport, error) {
	name = strings.TrimSpace(name)
	if name == DirectRouteName {
		// 强制直连：Proxy=nil，连接池独立缓存（sync.OnceValue）。
		return directTransport(), nil
	}
	if name == "" {
		return newTransport(), nil
	}
	routes.mu.RLock()
	addr, ok := routes.table[name]
	cached := routes.cache[name]
	known := len(routes.table)
	routes.mu.RUnlock()
	if !ok {
		if known == 0 {
			return nil, fmt.Errorf("账号绑定了出口线路 %q，但上游没有配置任何线路"+
				"（config.json 的 proxies 段）", name)
		}
		return nil, fmt.Errorf("未知的出口线路 %q（config.json 的 proxies 里没有它）", name)
	}
	if cached != nil {
		return cached, nil
	}
	u, err := normalizeProxyURL(addr)
	if err != nil {
		return nil, fmt.Errorf("出口线路 %q 的地址无效：%w", name, err)
	}
	tr := newTransport()
	tr.Proxy = http.ProxyURL(u)
	routes.mu.Lock()
	if routes.cache == nil {
		routes.cache = map[string]*http.Transport{}
	}
	routes.cache[name] = tr
	routes.mu.Unlock()
	return tr, nil
}

// closeIdler 实现该接口的 RoundTripper 支持清空空闲连接池（*http.Transport、
// http2.Transport 等均满足；测试注入的自定义 RoundTripper 可选择性实现）。
type closeIdler interface {
	CloseIdleConnections()
}

// roundTripCloseIdle 在传输层请求失败后清掉 rt 所属 Transport 的空闲连接池
// （kongjianguan 第 4 件：失败连接可能仍留在空闲池里，等 IdleConnTimeout 才
// 过期，下一个请求会继续捡到它）。
//
// 挂载点（任务书「评估挂载点：错误分类处理处」的结论）：错误分类（Classify）
// 只见业务信封——传输层失败根本没有 body 可分类（见 doJSON/ChatStreamContext
// 对 read body 失败的处理：不进 Classify、不罚号）。这类失败的正确处理正是
// 连接层的池清理，故挂在与 Do 并列的传输层出口（ChatStreamContext 的 Do 错误
// 分支），而非 applyErrorPolicy。
//
// 关闭是 best-effort：rt 为 nil 或未实现 closeIdler（如测试注入的 rtFunc）时
// 静默跳过。CloseIdleConnections 只关空闲连接，不影响在途请求；瞬时代价是
// 下个请求多一次 TCP+TLS 握手，与半死连接被复用卡 60s 的风险完全不成比例。
func roundTripCloseIdle(rt http.RoundTripper) {
	if rt == nil {
		return
	}
	if ci, ok := rt.(closeIdler); ok {
		ci.CloseIdleConnections()
	}
}
