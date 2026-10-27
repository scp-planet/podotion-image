package podotion

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func batchTestServer(t *testing.T, handler http.HandlerFunc) {
	t.Helper()
	server := httptest.NewTLSServer(handler)
	oldTransport, oldLoader := providerTransport, loadConfig
	providerTransport = server.Client().Transport
	loadConfig = func(string) (ProviderConfig, error) {
		return ProviderConfig{BaseURL: server.URL, Model: "test-model", BearerToken: "sk-test-secret", Name: SiteName, ProviderID: DirectProviderID}, nil
	}
	t.Cleanup(func() { server.Close(); providerTransport = oldTransport; loadConfig = oldLoader })
}

func successImage(w http.ResponseWriter) {
	json.NewEncoder(w).Encode(map[string]any{"data": []any{map[string]any{"b64_json": base64.StdEncoding.EncodeToString(fakePNG(64, 64))}}})
}

func batchOptions(t *testing.T, count int) (BatchOptions, BatchManifest) {
	t.Helper()
	dir := t.TempDir()
	manifest := BatchManifest{Schema: 1, BatchID: "test-batch"}
	for i := 0; i < count; i++ {
		manifest.Jobs = append(manifest.Jobs, BatchJob{JobID: fmt.Sprintf("job-%02d", i+1), Operation: "generate", Prompt: "identical variant prompt", Size: "auto"})
	}
	opts := BatchOptions{InputFile: filepath.Join(dir, "manifest.json"), OutputDir: filepath.Join(dir, "output"), StateScope: "test-scope"}
	writeBatchManifest(t, opts, manifest)
	return opts, manifest
}

func writeBatchManifest(t *testing.T, opts BatchOptions, manifest BatchManifest) {
	t.Helper()
	raw, _ := json.Marshal(manifest)
	writeFixture(t, opts.InputFile, raw)
}

func receiveWithin[T any](t *testing.T, channel <-chan T) T {
	t.Helper()
	select {
	case value := <-channel:
		return value
	case <-time.After(10 * time.Second):
		t.Fatal("synchronization barrier timed out")
	}
	var zero T
	return zero
}

func TestBatchTenConcurrentIdenticalVariantsResume(t *testing.T) {
	arrived := make(chan struct{}, 10)
	release := make(chan struct{})
	var calls atomic.Int32
	batchTestServer(t, func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		arrived <- struct{}{}
		select {
		case <-release:
			successImage(w)
		case <-r.Context().Done():
		}
	})
	opts, _ := batchOptions(t, 10)
	type outcome struct {
		result map[string]any
		err    error
	}
	done := make(chan outcome, 1)
	go func() { result, err := RunBatch(context.Background(), opts, nil); done <- outcome{result, err} }()
	// No response is released until all ten requests are inside the handler.
	for i := 0; i < 10; i++ {
		receiveWithin(t, arrived)
	}
	close(release)
	completed := receiveWithin(t, done)
	if completed.err != nil || completed.result["succeeded"] != 10 {
		t.Fatalf("batch: %v %v", completed.result, completed.err)
	}
	paths := map[string]bool{}
	jobs := completed.result["jobs"].([]map[string]any)
	for i, event := range jobs {
		if event["index"] != i+1 {
			t.Fatal("summary reordered")
		}
		result := event["result"].(map[string]any)
		path := result["images"].([]map[string]any)[0]["path"].(string)
		if paths[path] {
			t.Fatal("concurrent files overwritten")
		}
		paths[path] = true
		state := result["state_path"].(string)
		if state == statePath(opts.OutputDir, opts.StateScope) {
			t.Fatal("job overwrote parent last state")
		}
	}
	if _, err := ReadLastImage(opts.OutputDir, opts.StateScope); err == nil || !strings.Contains(err.Error(), "choose") {
		t.Fatalf("multiple images must require choice: %v", err)
	}
	resumed, err := RunBatch(context.Background(), opts, nil)
	if err != nil || calls.Load() != 10 {
		t.Fatalf("resume sent a new paid request: calls=%d err=%v", calls.Load(), err)
	}
	for _, event := range resumed["jobs"].([]map[string]any) {
		if event["result"].(map[string]any)["request"].(map[string]any)["reused"] != true {
			t.Fatal("resume did not reuse")
		}
	}
}

func TestBatchCompletionOrderAndFailureIsolation(t *testing.T) {
	opts, manifest := batchOptions(t, 3)
	for i := range manifest.Jobs {
		manifest.Jobs[i].Prompt = fmt.Sprintf("prompt-%d", i)
	}
	writeBatchManifest(t, opts, manifest)
	arrived := make(chan int, 3)
	events := make(chan int, 3)
	release := []chan struct{}{make(chan struct{}), make(chan struct{}), make(chan struct{})}
	batchTestServer(t, func(w http.ResponseWriter, r *http.Request) {
		var payload map[string]any
		json.NewDecoder(r.Body).Decode(&payload)
		index := 0
		fmt.Sscanf(payload["prompt"].(string), "prompt-%d", &index)
		arrived <- index
		select {
		case <-release[index]:
		case <-r.Context().Done():
			return
		}
		if index == 1 {
			http.Error(w, `{"error":{"message":"invalid sk-test-secret"}}`, 400)
			return
		}
		successImage(w)
	})
	done := make(chan error, 1)
	var summary map[string]any
	go func() {
		var err error
		summary, err = RunBatch(context.Background(), opts, func(event map[string]any) error { events <- event["index"].(int); return nil })
		done <- err
	}()
	for range manifest.Jobs {
		receiveWithin(t, arrived)
	}
	for _, i := range []int{2, 1, 0} {
		close(release[i])
		if got := receiveWithin(t, events); got != i+1 {
			t.Fatalf("event order: %d", got)
		}
	}
	if err := receiveWithin(t, done); err != ErrBatchFailed {
		t.Fatalf("expected partial failure, got %v", err)
	}
	if summary["succeeded"] != 2 || summary["failed"] != 1 {
		t.Fatalf("failure blocked independent jobs: %v", summary)
	}
	raw, _ := json.Marshal(summary)
	if strings.Contains(string(raw), "sk-test-secret") {
		t.Fatal("credential leaked")
	}
	lastRaw, err := os.ReadFile(statePath(opts.OutputDir, opts.StateScope))
	if err != nil {
		t.Fatal(err)
	}
	var last struct {
		Images []RecordImage `json:"images"`
	}
	json.Unmarshal(lastRaw, &last)
	ordered := summary["jobs"].([]map[string]any)
	for i, jobIndex := range []int{0, 2} {
		want := ordered[jobIndex]["result"].(map[string]any)["images"].([]map[string]any)[0]["path"]
		if last.Images[i].Path != want {
			t.Fatal("parent last state not in manifest order")
		}
	}
	if _, err := RunBatch(context.Background(), opts, nil); err != ErrBatchFailed {
		t.Fatalf("terminal failure must remain terminal: %v", err)
	}
}

func TestBatchPreflightZeroPOST(t *testing.T) {
	var calls atomic.Int32
	batchTestServer(t, func(w http.ResponseWriter, r *http.Request) { calls.Add(1); successImage(w) })
	for _, invalid := range []string{"corrupt", "missing-4k", "invalid-size", "invalid-model", "too-many-images"} {
		t.Run(invalid, func(t *testing.T) {
			opts, manifest := batchOptions(t, 3)
			switch invalid {
			case "corrupt":
				manifest.Jobs[2].Operation = "edit"
				manifest.Jobs[2].Images = []string{writeFixture(t, filepath.Join(filepath.Dir(opts.InputFile), "bad.jpg"), []byte{0xff, 0xd8, 0xff})}
			case "missing-4k":
				manifest.Jobs[2].Size = "4k"
			case "invalid-size":
				manifest.Jobs[2].Size = "999999x9"
			case "invalid-model":
				manifest.Jobs[2].Model = "invalid\nmodel"
			case "too-many-images":
				manifest.Jobs[2].Operation = "edit"
				for i := 0; i < 6; i++ {
					manifest.Jobs[2].Images = append(manifest.Jobs[2].Images, writeFixture(t, filepath.Join(filepath.Dir(opts.InputFile), fmt.Sprintf("%d.png", i)), encodedFixture(t, "png")))
				}
			}
			writeBatchManifest(t, opts, manifest)
			if _, err := RunBatch(context.Background(), opts, nil); err == nil {
				t.Fatal("preflight accepted invalid input")
			}
			if calls.Load() != 0 {
				t.Fatal("invalid batch sent POST")
			}
			records, err := iterRequestRecords(opts.OutputDir, opts.StateScope)
			if err != nil || len(records) != 0 {
				t.Fatal("invalid input recorded as submitted")
			}
		})
	}
}

func TestSingleInvalidImageNotSubmitted(t *testing.T) {
	var calls atomic.Int32
	batchTestServer(t, func(w http.ResponseWriter, r *http.Request) { calls.Add(1) })
	dir := t.TempDir()
	file := writeFixture(t, filepath.Join(dir, "bad.png"), fakePNG(20, 20))
	_, err := RunGeneration(context.Background(), GenerateOptions{Operation: "edit", Prompt: "test", InputImages: []string{file}, OutputDir: dir, StateScope: "scope", RequestKey: "invalid-input-key"})
	if err == nil || calls.Load() != 0 {
		t.Fatal("invalid input not rejected before POST")
	}
	status, _ := GetRequestStatus(dir, "invalid-input-key", "scope")
	if status["found"] != false {
		t.Fatal("local failure recorded as billable")
	}
}

func TestBatchFrozenEditAndUnknownRecovery(t *testing.T) {
	var calls atomic.Int32
	batchTestServer(t, func(w http.ResponseWriter, r *http.Request) { calls.Add(1); successImage(w) })
	opts, manifest := batchOptions(t, 2)
	source := writeFixture(t, filepath.Join(filepath.Dir(opts.InputFile), "source.png"), encodedFixture(t, "png"))
	manifest.Jobs[1].Operation = "edit"
	manifest.Jobs[1].Images = []string{source}
	writeBatchManifest(t, opts, manifest)
	if _, err := RunBatch(context.Background(), opts, nil); err != nil {
		t.Fatal(err)
	}
	os.Remove(source)
	// Simulate interruption after submission for one job; resume must use frozen
	// inputs/default model and must never resend this uncertain job.
	path, _ := requestRecordPath(opts.OutputDir, batchJobKey(manifest.BatchID, manifest.Jobs[1].JobID), opts.StateScope)
	record, _ := readRequestRecord(path)
	record.Status = "submitted"
	atomicWriteJSON(path, record)
	oldLoader := loadConfig
	loadConfig = func(value string) (ProviderConfig, error) {
		config, err := oldLoader(value)
		config.Model = "changed-default"
		return config, err
	}
	defer func() { loadConfig = oldLoader }()
	summary, err := RunBatch(context.Background(), opts, nil)
	if err != ErrBatchFailed || calls.Load() != 2 {
		t.Fatalf("uncertain request resubmitted: %v, calls %d", err, calls.Load())
	}
	first := summary["jobs"].([]map[string]any)[0]["result"].(map[string]any)
	if first["model"] != "test-model" {
		t.Fatal("model default not frozen")
	}
	record, _ = readRequestRecord(path)
	if record.Status != "outcome_unknown" {
		t.Fatalf("unknown status not retained: %s", record.Status)
	}
	manifest.Jobs[0].Prompt = "changed"
	writeBatchManifest(t, opts, manifest)
	if _, err := RunBatch(context.Background(), opts, nil); err == nil || calls.Load() != 2 {
		t.Fatal("changed batch identity was accepted")
	}
}

func TestRequestKeyLockDifferentFingerprints(t *testing.T) {
	arrived := make(chan struct{}, 1)
	release := make(chan struct{})
	batchTestServer(t, func(w http.ResponseWriter, r *http.Request) { arrived <- struct{}{}; <-release; successImage(w) })
	opts := GenerateOptions{Operation: "generate", Prompt: "first prompt", OutputDir: t.TempDir(), StateScope: "scope", RequestKey: "one-shared-request-key"}
	done := make(chan error, 1)
	go func() { _, err := RunGeneration(context.Background(), opts); done <- err }()
	receiveWithin(t, arrived)
	changed := opts
	changed.Prompt = "different fingerprint"
	_, err := RunGeneration(context.Background(), changed)
	close(release)
	if err == nil || err.(*ProviderError).Kind != "request_in_progress" {
		t.Fatalf("same key concurrently submitted: %v", err)
	}
	if err := receiveWithin(t, done); err != nil {
		t.Fatal(err)
	}
}

func TestBatchConcurrencyLimitAndFrozenInputs(t *testing.T) {
	var active, maxActive atomic.Int32
	arrived := make(chan struct{}, 5)
	release := make(chan struct{}, 5)
	batchTestServer(t, func(w http.ResponseWriter, r *http.Request) {
		current := active.Add(1)
		defer active.Add(-1)
		for {
			old := maxActive.Load()
			if current <= old || maxActive.CompareAndSwap(old, current) {
				break
			}
		}
		arrived <- struct{}{}
		select {
		case <-release:
			successImage(w)
		case <-r.Context().Done():
		}
	})
	opts, _ := batchOptions(t, 5)
	opts.Concurrency = 2
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { _, err := RunBatch(ctx, opts, nil); done <- err }()
	for _, wave := range []int{2, 2, 1} {
		for i := 0; i < wave; i++ {
			receiveWithin(t, arrived)
		}
		for i := 0; i < wave; i++ {
			release <- struct{}{}
		}
	}
	if err := receiveWithin(t, done); err != nil {
		t.Fatal(err)
	}
	if maxActive.Load() != 2 {
		t.Fatalf("concurrency cap: %d", maxActive.Load())
	}
}

func TestBatchInputSnapshotSurvivesOverwrite(t *testing.T) {
	original := encodedFixture(t, "jpeg")
	batchTestServer(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/edits") {
			reader, err := r.MultipartReader()
			if err != nil {
				t.Error(err)
				return
			}
			for {
				part, err := reader.NextPart()
				if err == io.EOF {
					break
				}
				if err != nil {
					t.Error(err)
					return
				}
				data, _ := io.ReadAll(part)
				if part.FileName() != "" && !bytes.Equal(data, original) {
					t.Error("input changed after batch preflight")
				}
			}
		}
		successImage(w)
	})
	opts, manifest := batchOptions(t, 2)
	opts.Concurrency = 1
	path := writeFixture(t, filepath.Join(filepath.Dir(opts.InputFile), "input.jpg"), original)
	manifest.Jobs[1].Operation = "edit"
	manifest.Jobs[1].Images = []string{path}
	writeBatchManifest(t, opts, manifest)
	_, err := RunBatch(context.Background(), opts, func(event map[string]any) error {
		if event["index"] == 1 {
			return os.WriteFile(path, []byte("overwritten"), 0o600)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

func TestBatchDisconnectOnlyBlocksUncertainJob(t *testing.T) {
	var calls atomic.Int32
	batchTestServer(t, func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var payload map[string]any
		json.NewDecoder(r.Body).Decode(&payload)
		if payload["prompt"] == "disconnect" {
			connection, _, err := w.(http.Hijacker).Hijack()
			if err != nil {
				t.Error(err)
				return
			}
			connection.Close()
			return
		}
		successImage(w)
	})
	opts, manifest := batchOptions(t, 3)
	manifest.Jobs[1].Prompt = "disconnect"
	writeBatchManifest(t, opts, manifest)
	summary, err := RunBatch(context.Background(), opts, nil)
	if err != ErrBatchFailed || summary["succeeded"] != 2 || calls.Load() != 3 {
		t.Fatalf("disconnect blocked independent jobs: %v %v", summary, err)
	}
	key := batchJobKey(manifest.BatchID, manifest.Jobs[1].JobID)
	status, err := GetRequestStatus(opts.OutputDir, key, opts.StateScope)
	if err != nil || status["effective_status"] != "outcome_unknown" {
		t.Fatalf("unknown outcome lost: %v %v", status, err)
	}
	if _, err := RunBatch(context.Background(), opts, nil); err != ErrBatchFailed || calls.Load() != 3 {
		t.Fatal("uncertain request resent")
	}
}
