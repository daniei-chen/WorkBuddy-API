package redisstore

// FileBinds adds a private, atomic local session mirror without changing the
// account pool or session Router algorithms. Other operations use the delegate.
import (
 "encoding/json"
 "fmt"
 "log"
 "os"
 "path/filepath"
 "sync"
 "time"
)

type fileBind struct { UID string `json:"uid"`; Expires int64 `json:"expires"` }
type FileBinds struct {
 Store
 mu sync.Mutex
 path string
 binds map[string]fileBind
}

func NewFileBinds(path string, delegate Store) (*FileBinds,error) {
 if delegate==nil { delegate=Noop{} }
 f:=&FileBinds{Store:delegate,path:path,binds:map[string]fileBind{}}
 if err:=os.MkdirAll(filepath.Dir(path),0700);err!=nil{return nil,err}
 if info,err:=os.Lstat(path);err==nil {
  if !info.Mode().IsRegular(){return nil,fmt.Errorf("session mirror must be a regular file")}
  if info.Size()>16*1024*1024{return nil,fmt.Errorf("session mirror too large")}
 } else if !os.IsNotExist(err){return nil,err}
 raw,err:=os.ReadFile(path)
 if err==nil {
  if len(raw)>16*1024*1024{return nil,fmt.Errorf("session mirror too large")}
  if err=json.Unmarshal(raw,&f.binds);err!=nil{return nil,fmt.Errorf("invalid session mirror: %w",err)}
 } else if !os.IsNotExist(err){return nil,err}
 f.pruneLocked()
 if len(f.binds)>20000{return nil,fmt.Errorf("session mirror has too many bindings")}
 return f,nil
}
func (f *FileBinds) pruneLocked(){
 now:=time.Now().UnixNano()
 for k,v:=range f.binds {if v.Expires<=now{delete(f.binds,k)}}
}
func (f *FileBinds) persistLocked() error {
 f.pruneLocked()
 raw,err:=json.Marshal(f.binds);if err!=nil{return err}
 tmp,err:=os.CreateTemp(filepath.Dir(f.path),".session-binds-");if err!=nil{return err}
 name:=tmp.Name();defer os.Remove(name)
 if err=tmp.Chmod(0600);err==nil{_,err=tmp.Write(raw)}
 if err==nil{err=tmp.Sync()}
 closeErr:=tmp.Close();if err==nil{err=closeErr};if err!=nil{return err}
 if err=os.Rename(name,f.path);err!=nil{return err}
 dir,err:=os.Open(filepath.Dir(f.path));if err==nil{err=dir.Sync();dir.Close()}
 return err
}
func (f *FileBinds) SetBind(key,uid string,ttl time.Duration){
 f.mu.Lock()
 f.binds[key]=fileBind{uid,time.Now().Add(ttl).UnixNano()}
 // Bound this mirror. Removing the earliest expiry does not change live Router
 // selection; it only limits how many bindings are eligible for restart recovery.
 if len(f.binds)>20000 {
  oldest:="";var expiry int64
  for k,v:=range f.binds{if oldest==""||v.Expires<expiry{oldest=k;expiry=v.Expires}}
  delete(f.binds,oldest)
 }
 err:=f.persistLocked();f.mu.Unlock()
 if err!=nil{log.Printf("[session] local mirror write failed: %v",err)}
 f.Store.SetBind(key,uid,ttl)
}
func (f *FileBinds) DelBind(key string){
 f.mu.Lock();delete(f.binds,key);err:=f.persistLocked();f.mu.Unlock()
 if err!=nil{log.Printf("[session] local mirror delete failed: %v",err)}
 f.Store.DelBind(key)
}
func (f *FileBinds) LoadBinds() map[string]string {
 f.mu.Lock();defer f.mu.Unlock();f.pruneLocked()
 result:=map[string]string{}
 for k,v:=range f.binds{result[k]=v.UID}
 // The local TTL-aware mirror is authoritative for sessions. Do not resurrect
 // stale Redis entries whose remaining lifetime cannot be determined here.
 return result
}
type ExpiringBind struct { UID string; Expires time.Time }
func (f *FileBinds) LoadExpiringBinds() map[string]ExpiringBind {
 f.mu.Lock();defer f.mu.Unlock();f.pruneLocked()
 result:=map[string]ExpiringBind{}
 for k,v:=range f.binds{result[k]=ExpiringBind{UID:v.UID,Expires:time.Unix(0,v.Expires)}}
 return result
}
func (f *FileBinds) Close()error{
 f.mu.Lock();err:=f.persistLocked();f.mu.Unlock()
 other:=f.Store.Close();if err!=nil{return err};return other
}
