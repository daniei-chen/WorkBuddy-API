package scheduler

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"workbuddy2api/internal/auth"
	"workbuddy2api/internal/pool"
	"workbuddy2api/internal/upstream"
)

// 这一组测试锁的是「睡眠 / 休眠跨过槽位」这个**不报错**的故障：
// 定时器走单调时钟，机器睡眠期间不推进，一个睡到槽位的长 timer 会被整体顺延；
// 期间若机器醒着（睡醒之后到槽位那一小时）也没人再对一次墙钟，槽位就被整段跳过。
// macOS 实测：01:14 起睡 7h23m，09:00 的签到到 15:02 仍未跑。

func TestWaitForSlotReturnsImmediatelyWhenPast(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	start := time.Now()
	if !waitForSlot(ctx, start.Add(-time.Hour)) {
		t.Fatal("槽位已过却报失败（应立刻放行）")
	}
	if elapsed := time.Since(start); elapsed > 100*time.Millisecond {
		t.Fatalf("槽位已过还等了 %v", elapsed)
	}
}

func TestWaitForSlotWaitsUntilDue(t *testing.T) {
	oldPoll := slotPoll
	slotPoll = 10 * time.Millisecond
	defer func() { slotPoll = oldPoll }()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	start := time.Now()
	due := start.Add(200 * time.Millisecond)
	if !waitForSlot(ctx, due) {
		t.Fatal("到点却没等到")
	}
	if elapsed := time.Since(start); elapsed < 150*time.Millisecond {
		t.Fatalf("提前放行：只等了 %v（应等到 %v）", elapsed, 200*time.Millisecond)
	}
}

func TestWaitForSlotContextCancel(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if waitForSlot(ctx, time.Now().Add(time.Hour)) {
		t.Fatal("ctx 取消后仍返回 true（会继续派发）")
	}
}

// dueSlots 从 cursor 往后枚举，所以「睡过 21:00 与 01:00 之后，09:00 的签到仍在
// 醒来时补上」——修前 Run 从 time.Now() 重算，09:00 会被直接跳过（只能等第二天）。
func TestDueSlotsCatchesSlotsCrossedBySleep(t *testing.T) {
	s := New(Config{
		CheckinHours:   []int{9},
		TravelHours:    []int{9}, // 与签到同刻：验证同刻合并
		ActivityHours:  []int{10},
		KeepaliveHours: []int{21},
		SchoolHours:    []int{12},
		CatHours:       []int{1},
	})
	cursor := time.Date(2026, 9, 27, 20, 0, 0, 0, time.Local)
	now := time.Date(2026, 9, 28, 9, 5, 0, 0, time.Local)

	got := s.dueSlots(cursor, now)
	var planned []string
	for _, d := range got {
		planned = append(planned, d.planned.Format("01-02 15:04"))
	}
	// 20:00 睡到次日 09:05：跨过的槽位逐个补，且**不含**还没到的 10:00。
	want := []string{"09-27 21:00", "09-28 01:00", "09-28 09:00"}
	if len(planned) != len(want) {
		t.Fatalf("补跑槽位=%v，期望 %v", planned, want)
	}
	for i := range want {
		if planned[i] != want[i] {
			t.Fatalf("补跑槽位=%v，期望 %v（顺序必须是时间升序）", planned, want)
		}
	}
	// 09:00 的签到与旅行同刻，合并成一批
	if len(got[2].kinds) != 2 || !hasKind(got[2].kinds, taskCheckin) || !hasKind(got[2].kinds, taskTravel) {
		t.Fatalf("同刻多类任务没有合并：%v", got[2].kinds)
	}
}

func TestDueSlotsSkipsDisabledAndDoesNotRepeatCursor(t *testing.T) {
	// 零值 Config 里其余五类是「启用」的（见 Config 的说明），所以这里只断言
	// 「签到这一类」（被显式禁用）不出现在补跑列表里。
	s := New(Config{CheckinHours: []int{9}, CheckinDisabled: true})
	cursor := time.Date(2026, 9, 27, 20, 0, 0, 0, time.Local)
	now := time.Date(2026, 9, 28, 9, 5, 0, 0, time.Local)
	for _, d := range s.dueSlots(cursor, now) {
		if hasKind(d.kinds, taskCheckin) {
			t.Fatalf("签到被禁用却仍有补跑：%v", d)
		}
	}
	// cursor == now：没有任何「已过」的槽位（刚派发完的那个不能重复派发）
	s2 := New(Config{CheckinHours: []int{9}})
	at := time.Date(2026, 9, 28, 9, 0, 0, 0, time.Local)
	if got := s2.dueSlots(at, at); len(got) != 0 {
		t.Fatalf("刚派发过的槽位被重复派发：%v", got)
	}
}

// 端到端（假时钟）：机器从 20:00 睡到次日 09:05，醒来后 09:00 的签到必须补跑。
func TestRunCatchesUpAfterSleep(t *testing.T) {
	var clock atomic.Int64
	base := time.Date(2026, 9, 27, 20, 0, 0, 0, time.Local)
	clock.Store(base.UnixNano())

	oldNow, oldPoll, oldGrace := nowFunc, slotPoll, wakeupGraceDelay
	nowFunc = func() time.Time { return time.Unix(0, clock.Load()).In(base.Location()) }
	slotPoll = 5 * time.Millisecond
	wakeupGraceDelay = 0
	defer func() { nowFunc, slotPoll, wakeupGraceDelay = oldNow, oldPoll, oldGrace }()

	f := &fakeUpstream{}
	srv := f.server()
	defer srv.Close()

	p := pool.New("")
	// ExpiresAt=1 → 保活路径会真的去刷新 token（与既有 keepalive 测试同口径）
	p.Add(&auth.Auth{UID: "u1", AccessToken: "old", RefreshToken: "rt", ExpiresAt: 1})
	up := &upstream.Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	s := New(Config{
		Pool: p, Upstream: up,
		CheckinHours:   []int{9},
		KeepaliveHours: []int{21},
		TravelDisabled: true, ActivityDisabled: true, SchoolDisabled: true, CatDisabled: true,
	})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { s.Run(ctx); close(done) }()

	time.Sleep(200 * time.Millisecond)                // 让 Run 进入等待（等 21:00）
	clock.Add(int64(13*time.Hour + 5*time.Minute))    // 睡一夜：20:00 → 次日 09:05

	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) && f.checkinCalls.Load() == 0 {
		time.Sleep(20 * time.Millisecond)
	}
	if f.checkinCalls.Load() == 0 {
		t.Fatal("睡醒后没有补跑 09:00 的签到 —— 睡眠跨过的槽位被跳过了")
	}
	if f.refreshCalls.Load() == 0 {
		t.Fatal("睡醒后没有补跑 21:00 的 token 保活（跨过的槽位应逐个补）")
	}
	cancel()
	<-done
}

// 长时间离线（超出 catchupWindow）不该把几天的任务一次性全跑一遍。
func TestRunDoesNotCatchUpBeyondWindow(t *testing.T) {
	var clock atomic.Int64
	base := time.Date(2026, 9, 20, 20, 0, 0, 0, time.Local)
	clock.Store(base.UnixNano())

	oldNow, oldPoll, oldGrace := nowFunc, slotPoll, wakeupGraceDelay
	nowFunc = func() time.Time { return time.Unix(0, clock.Load()).In(base.Location()) }
	slotPoll = 5 * time.Millisecond
	wakeupGraceDelay = 0
	defer func() { nowFunc, slotPoll, wakeupGraceDelay = oldNow, oldPoll, oldGrace }()

	f := &fakeUpstream{}
	srv := f.server()
	defer srv.Close()
	p := pool.New("")
	p.Add(&auth.Auth{UID: "u1", AccessToken: "at", RefreshToken: "rt", ExpiresAt: 9999999999})
	up := &upstream.Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	s := New(Config{
		Pool: p, Upstream: up,
		CheckinHours: []int{9}, TravelDisabled: true, ActivityDisabled: true,
		KeepaliveDisabled: true, SchoolDisabled: true, CatDisabled: true,
	})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { s.Run(ctx); close(done) }()

	time.Sleep(200 * time.Millisecond)
	clock.Add(int64(7 * 24 * time.Hour)) // 关机一周
	time.Sleep(400 * time.Millisecond)   // 给它几次轮询的机会
	// 一周里的槽位都超出补跑窗口，只有**最近那一个**（11 小时前）该跑，
	// 而且只能跑一次 —— 不能把一周的签到一次性全部补一遍。
	if n := f.checkinCalls.Load(); n != 1 {
		t.Fatalf("checkin=%d 次，期望恰好 1 次（只补最近一个槽位，不重放一周）", n)
	}
	cancel()
	<-done
}
