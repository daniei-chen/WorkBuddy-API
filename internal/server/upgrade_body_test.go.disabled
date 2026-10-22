package server

import (
    "net/http"
    "net/http/httptest"
    "strings"
    "testing"
)

func TestManagedBodyLimitDeclaredAndChunked(t *testing.T) {
    for _, length := range []int64{-1, 5, 100} {
        handler := NewHandler(Config{MaxBodyBytes: 16})
        req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(strings.Repeat("x", 100)))
        req.ContentLength = length
        recorder := httptest.NewRecorder()
        handler.ServeHTTP(recorder, req)
        if recorder.Code != http.StatusRequestEntityTooLarge {
            t.Fatalf("ContentLength=%d status=%d want 413", length, recorder.Code)
        }
    }
}
