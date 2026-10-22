package config

import (
	"encoding/json"
	"testing"
)

func TestRandomScheduleConfigDefaultAndBounds(t *testing.T) {
	for _, minutes := range []int{0, 1, 30, 60} {
		s := DefaultSchedule()
		s.RandomDelayMinutes = minutes
		if err := s.Normalize(); err != nil {
			t.Fatalf("valid delay %d: %v", minutes, err)
		}
	}
	for _, minutes := range []int{-1, 61} {
		s := DefaultSchedule()
		s.RandomDelayMinutes = minutes
		if err := s.Normalize(); err == nil {
			t.Fatalf("invalid delay %d accepted", minutes)
		}
	}
	s := DefaultSchedule()
	if err := json.Unmarshal([]byte(`{"checkin_hours":[9,21]}`), &s); err != nil {
		t.Fatal(err)
	}
	if s.RandomDelayMinutes != 0 {
		t.Fatal("old config unexpectedly enabled jitter")
	}
	if err := json.Unmarshal([]byte(`{"random_delay_minutes":60}`), &s); err != nil {
		t.Fatal(err)
	}
	if s.RandomDelayMinutes != 60 {
		t.Fatal("delay config was not loaded")
	}
}

func TestAccountDelayConfigBoundsAndCompatibility(t *testing.T) {
	for _, pair := range [][2]int{{0, 0}, {0, 120}, {30, 120}, {600, 600}} {
		s := DefaultSchedule()
		s.AccountDelayMinSeconds = pair[0]
		s.AccountDelayMaxSeconds = pair[1]
		if err := s.Normalize(); err != nil {
			t.Fatalf("valid range %v: %v", pair, err)
		}
	}
	for _, pair := range [][2]int{{-1, 120}, {30, 29}, {0, 601}, {0, -1}} {
		s := DefaultSchedule()
		s.AccountDelayMinSeconds = pair[0]
		s.AccountDelayMaxSeconds = pair[1]
		if err := s.Normalize(); err == nil {
			t.Fatalf("invalid range accepted: %v", pair)
		}
	}
	s := DefaultSchedule()
	if s.AccountDelayMinSeconds != 0 || s.AccountDelayMaxSeconds != 0 {
		t.Fatal("default enabled account staggering")
	}
}
