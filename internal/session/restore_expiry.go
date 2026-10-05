package session

import (
 "time"
 "workbuddy2api/internal/redisstore"
)

// LoadFromStoreWithExpiry preserves the remaining lifetime of local bindings.
// The existing selection, rotation, touch and GC algorithms are unchanged.
func (r *Router) LoadFromStoreWithExpiry() {
 loader,ok:=r.cfg.Store.(interface{LoadExpiringBinds() map[string]redisstore.ExpiringBind})
 if !ok{r.LoadFromStore();return}
 binds:=loader.LoadExpiringBinds()
 now:=time.Now()
 r.mu.Lock();defer r.mu.Unlock()
 for key,b:=range binds {
  if _,exists:=r.entries[key];exists||!b.Expires.After(now){continue}
  active:=b.Expires.Add(-r.cfg.TTL)
  if active.After(now){active=now}
  r.entries[key]=entry{uid:b.UID,lastActive:active}
 }
}
