// 慢速降权（本地定制）：上游按账号排队（实测同一时刻不同账号 TTFB 差 4~5 倍，
// 且慢号集合随拥堵时段轮换），而「慢而 200」的账号永远触发不了现有惩罚——连败
// 降权/熔断都只数失败。本机制以流式成功请求的 TTFB 为信号，连续超阈的账号临时
// 出池，选号自动绕开慢号；账号排队恢复后经半开探测自动回池。
//
// 单笔样本「慢」的双门槛判定（isSlowTTFB）：
//   - 绝对下限 slowTTFB（默认 45s）：前缀缓存 miss / 长 prompt 的正常慢是秒级，
//     到不了这个量级——只有上游排队（生成已在跑、帧被 hold 到排队结束才吐）才
//     会越线，阈值本身就把「缓存 miss 假慢」排除在触发域之外；
//   - 相对门槛 slowRelativeTTFB × 池内近期 TTFB 中位数：上游整体拥堵时段全池
//     均匀变慢，人人越绝对线但无人越相对线 → 谁都不降（防雪崩：全池出池只会把
//     流量赶进兜底 lane 轮询，白白放弃 top5 加权与粘性）；拥堵中被单独卡死排队
//     的号（远慢于同期其他号）才越线。环样本不足 slowRingMinForRelative 时
//     （冷启动/重启初期）只按绝对线判定，窗口极短（slowRingMinForRelative 笔）。
//
// 滞回与恢复：
//   - 连续 slowStreak 笔慢样本才降权，中间夹一笔健康样本即清零（streak 是连续
//     口径，非窗口累计）；降权时长 slowCooldown 固定，降权期内再次达阈**不延长**
//     （只清计数，与 NoteFailures 同哲学——防偶发尖峰把降权越堆越厚）；
//   - slowUntil 并入 healthy()（normal 选号跳过、粘性直取失败自动解绑回落轮换）
//     与 expiry()（兜底 lane 仍可选 = 半开探测）；探测样本健康即清 slowUntil
//     立即回池（与 NoteSuccess 清 degradeUntil「成功是恢复的最强证据」同口径）。
//
// 观测范围：仅流式成功路径喂样本（handler 流式成功块）——TTFB 只有流式有真实
// 观测（非流式拿到的是完整响应，无法区分「排队慢」与「生成慢」，喂进去只会引入
// 偏差）。样本与 streak 均为运行态（不持久化，与熔断 fails 同口径）：重启后最多
// slowStreak 笔请求重新学出慢号，代价可接受。
package pool

import (
	"log"
	"sort"
	"time"

	"workbuddy2api/internal/logfmt"
)

// 慢速降权默认参数与常量（SetSlowDegrade 注入可覆盖；风格同 defaultDegrade*）。
const (
	// defaultSlowTTFB 绝对下限。取 45s：正常请求 TTFB p95 在 30s 内（实测 48h
	// 全池分布），排队型慢号在拥堵期稳定 60~100s——45s 分开两个分布且离两者都有
	// 余量。可经 config pool.slow_ttfb 覆盖。
	defaultSlowTTFB = 45 * time.Second
	// defaultSlowStreak 连续慢样本触发阈值。取 3：与熔断阈值同级——样本是成功
	// 请求（有真实流量成本），阈值过低会因偶发尖峰误出池；过高则慢号多咬 2 笔。
	defaultSlowStreak = 3
	// defaultSlowCooldown 降权时长。取 30m：上游拥堵时段实测持续数小时，短于
	// 时段长度意味着频繁重探（每轮 3 笔慢请求）；长于熔断基数（30m）无意义——
	// 探测语义保证了误判最多 30m 自愈。可经 config pool.slow_cooldown 覆盖。
	defaultSlowCooldown = 30 * time.Minute
	// slowRelativeTTFB 相对门槛倍数：TTFB 须 ≥ 3× 池内中位数才判慢。3 是
	// 「显著慢于同期其他号」与「容忍分布右尾」之间的折中（实测拥堵期正常号
	// 与慢号差距 4~5 倍，同号跨时段波动 2~3 倍）。
	slowRelativeTTFB = 3.0
	// slowRingSize 池级 TTFB 观测环容量。64 笔 ≈ 当前流量下约半小时的观测，
	// 中位数对个别慢号样本稳健；再大只是让中位数更迟钝，无收益。
	slowRingSize = 64
	// slowRingMinForRelative 相对门槛生效的最小环样本数。冷启动/重启初期样本
	// 稀疏，中位数无统计意义，只按绝对线判定。
	slowRingMinForRelative = 8
)

// slowDegradeReason 慢速降权写 reason 的固定文案（/status 透出；与 degradeReason
// 共用 Reason 字段的口径一致，不新增台账字段）。
const slowDegradeReason = "slow responses"

// NoteSpeed 记录一次成功请求的 TTFB 观测（慢速降权唯一喂入口）。ttfb<=0（无
// 观测，如上游未吐帧就断连）直接忽略。内部分三步：进池级观测环（相对门槛的
// 分母）、按双门槛判慢、推进账号 streak/降权状态机。未启用的池只进环不判定。
func (p *Pool) NoteSpeed(uid string, ttfb time.Duration) {
	if ttfb <= 0 {
		return
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	p.slowRingAppendLocked(ttfb)
	if !p.slowEnabled {
		return
	}
	e, ok := p.byUID[uid]
	if !ok {
		return
	}
	if !p.isSlowTTFBLocked(ttfb) {
		if e.slowStreak != 0 {
			e.slowStreak = 0 // 健康样本打断连续口径
		}
		// 半开探测恢复：降权期内出现健康样本 = 兜底探测成功，立即回池。
		// 与 NoteSuccess 清 degradeUntil「成功是恢复的最强证据」同口径；若误判
		// （账号仍时快时慢），slowStreak 重新累计满阈值会再次降权。
		if !e.slowUntil.IsZero() && time.Now().Before(e.slowUntil) {
			e.slowUntil = time.Time{}
			log.Printf("[pool] slow-degrade cleared (healthy probe ttfb=%s) acct=%s",
				ttfb.Truncate(time.Millisecond), logfmt.Label(e.a.UID, e.a.Nickname))
		}
		return
	}
	e.slowStreak++
	if e.slowStreak < p.slowStreakN {
		return
	}
	e.slowStreak = 0 // 达阈清零供下一轮重新累计（同 NoteFailures）
	now := time.Now()
	if !e.slowUntil.IsZero() && now.Before(e.slowUntil) {
		return // 降权期内再次达阈：不延长、不翻倍（防尖峰把降权越堆越厚）
	}
	e.slowUntil = now.Add(p.slowCooldown)
	log.Printf("WARN: [pool] slow-degrade uid=%s until=%s (ttfb=%s ≥ %s, %d×median %s, %d consecutive)",
		logfmt.UID8(uid), e.slowUntil.Format("15:04:05"),
		ttfb.Truncate(time.Millisecond), p.slowTTFB, int(slowRelativeTTFB),
		p.slowRingMedianLocked().Truncate(time.Millisecond), p.slowStreakN)
}

// isSlowTTFBLocked 单笔样本的双门槛判定（见包注释）。调用方必须已持有 p.mu。
// 环样本不足时中位数不可信（可能全来自同一个慢号），只按绝对线判定——冷启动
// 窗口内最多误伤 slowRingMinForRelative 笔请求对应的号，且健康探测会立即回池。
func (p *Pool) isSlowTTFBLocked(ttfb time.Duration) bool {
	if ttfb < p.slowTTFB {
		return false
	}
	if p.slowRingCount < slowRingMinForRelative {
		return true
	}
	return float64(ttfb) >= slowRelativeTTFB*float64(p.slowRingMedianLocked())
}

// slowRingAppendLocked 追加一笔 TTFB 观测进池级环形缓冲。调用方必须已持有 p.mu。
func (p *Pool) slowRingAppendLocked(ttfb time.Duration) {
	p.slowRing[p.slowRingIdx] = ttfb
	p.slowRingIdx = (p.slowRingIdx + 1) % len(p.slowRing)
	if p.slowRingCount < len(p.slowRing) {
		p.slowRingCount++
	}
}

// slowRingMedianLocked 返回环内观测的中位数（副本排序，仅在「候选慢样本」路径
// 调用——ttfb 越绝对线才进这里，频率低，O(n log n) 可接受）。调用方必须已持有
// p.mu，且 slowRingCount > 0。
func (p *Pool) slowRingMedianLocked() time.Duration {
	vals := make([]time.Duration, p.slowRingCount)
	copy(vals, p.slowRing[:p.slowRingCount])
	sort.Slice(vals, func(i, j int) bool { return vals[i] < vals[j] })
	return vals[len(vals)/2]
}

// slowStateOf 曝露账号的慢速降权运行态（包内测试 helper；/status 走 statusOf）。
func (p *Pool) slowStateOf(uid string) (streak int, slowUntil time.Time, ok bool) {
	p.mu.RLock()
	defer p.mu.RUnlock()
	e, exists := p.byUID[uid]
	if !exists {
		return 0, time.Time{}, false
	}
	return e.slowStreak, e.slowUntil, true
}
