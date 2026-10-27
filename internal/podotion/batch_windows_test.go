//go:build windows

package podotion

import (
	"context"
	"net/http"
	"sync/atomic"
	"syscall"
	"testing"
)

func TestBatchIgnoresUnrelatedSharingViolation(t *testing.T) {
	var calls atomic.Int32
	batchTestServer(t, func(w http.ResponseWriter, r *http.Request) { calls.Add(1); successImage(w) })
	opts, _ := batchOptions(t, 3)
	path, err := requestRecordPath(opts.OutputDir, "another-batch-request", opts.StateScope)
	if err != nil {
		t.Fatal(err)
	}
	if err := atomicWriteJSON(path, map[string]string{"status": "submitted"}); err != nil {
		t.Fatal(err)
	}
	name, err := syscall.UTF16PtrFromString(path)
	if err != nil {
		t.Fatal(err)
	}
	// Hold an unrelated record open without sharing, reproducing the Windows
	// error observed when workers scanned a record being atomically replaced.
	handle, err := syscall.CreateFile(name, syscall.GENERIC_READ, 0, nil, syscall.OPEN_EXISTING, syscall.FILE_ATTRIBUTE_NORMAL, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer syscall.CloseHandle(handle)
	summary, err := RunBatch(context.Background(), opts, nil)
	if err != nil || summary["succeeded"] != 3 || calls.Load() != 3 {
		t.Fatalf("unrelated sharing violation blocked batch: %v %v calls=%d", summary, err, calls.Load())
	}
	if _, err := RunBatch(context.Background(), opts, nil); err != nil || calls.Load() != 3 {
		t.Fatalf("resume resent independent jobs: %v calls=%d", err, calls.Load())
	}
}
