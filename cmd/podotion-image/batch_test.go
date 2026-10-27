package main

import (
	"bufio"
	"bytes"
	"encoding/base64"
	"encoding/json"
	"image"
	"image/png"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
)

type testRoundTripper func(*http.Request) (*http.Response, error)

func (f testRoundTripper) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestBatchCLIJSONLinesAndBlackBoxResume(t *testing.T) {
	dir := t.TempDir()
	config := filepath.Join(dir, "provider.toml")
	os.WriteFile(config, []byte("base_url = \"https://ai.podotion.com/v1\"\nmodel = \"test-model\"\nPodotionImageSk = \"sk-test-secret\"\n"), 0o600)
	manifest := filepath.Join(dir, "manifest.json")
	os.WriteFile(manifest, []byte(`{"schema":1,"batch_id":"cli-batch","jobs":[{"job_id":"a","operation":"generate","prompt":"success"},{"job_id":"b","operation":"generate","prompt":"fail"},{"job_id":"c","operation":"generate","prompt":"success"}]}`), 0o600)
	var calls atomic.Int32
	var pngBytes bytes.Buffer
	png.Encode(&pngBytes, image.NewRGBA(image.Rect(0, 0, 2, 2)))
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var payload map[string]any
		json.NewDecoder(r.Body).Decode(&payload)
		if payload["prompt"] == "fail" {
			http.Error(w, "bad input", 400)
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"data": []any{map[string]any{"b64_json": base64.StdEncoding.EncodeToString(pngBytes.Bytes())}}})
	}))
	defer server.Close()
	old := http.DefaultTransport
	http.DefaultTransport = testRoundTripper(func(r *http.Request) (*http.Response, error) {
		clone := r.Clone(r.Context())
		urlCopy := *r.URL
		clone.URL = &urlCopy
		clone.URL.Host = strings.TrimPrefix(server.URL, "https://")
		return server.Client().Transport.RoundTrip(clone)
	})
	defer func() { http.DefaultTransport = old }()
	args := []string{"batch", "--credential-file", config, "--input-file", manifest, "--output-dir", filepath.Join(dir, "output"), "--state-scope", "cli-scope", "--concurrency", "2"}
	var stdout, stderr bytes.Buffer
	if code := run(args, strings.NewReader(""), &stdout, &stderr); code != 1 {
		t.Fatalf("expected partial failure: code %d stderr %s", code, stderr.String())
	}
	assertBatchJSONLines(t, stdout.String())
	if calls.Load() != 3 {
		t.Fatalf("expected exactly 3 POSTs, got %d", calls.Load())
	}
	// The subprocess has no fake transport: resume must read durable successful
	// and terminal records without touching the upstream service.
	out, errOut, code := runCLI(t, []string{"CODEX_HOME=" + t.TempDir()}, "", args...)
	if code != 1 || errOut != "" {
		t.Fatalf("resume CLI: %d %s", code, errOut)
	}
	assertBatchJSONLines(t, out)
	if calls.Load() != 3 {
		t.Fatal("resume contacted provider")
	}
}

func assertBatchJSONLines(t *testing.T, text string) {
	t.Helper()
	scanner := bufio.NewScanner(strings.NewReader(text))
	count := 0
	for scanner.Scan() {
		var event map[string]any
		if err := json.Unmarshal(scanner.Bytes(), &event); err != nil {
			t.Fatalf("stdout is not JSONL: %v", err)
		}
		if strings.Contains(scanner.Text(), "sk-test-secret") {
			t.Fatal("credential leaked")
		}
		if event["event"] == "batch_completed" {
			if event["succeeded"] != float64(2) || event["failed"] != float64(1) || event["ok"] != false {
				t.Fatalf("summary: %v", event)
			}
			jobs := event["jobs"].([]any)
			for i, job := range jobs {
				if job.(map[string]any)["index"] != float64(i+1) {
					t.Fatal("summary lost manifest order")
				}
			}
		} else if event["event"] != "job_completed" {
			t.Fatalf("invalid event: %v", event)
		}
		count++
	}
	if scanner.Err() != nil || count != 4 {
		t.Fatalf("expected three completion events plus summary, got %d", count)
	}
}

func TestBatchCLIRejectsInvalidBeforeOutput(t *testing.T) {
	for _, args := range [][]string{{"batch"}, {"batch", "--concurrency", "0", "--input-file", "missing"}, {"batch", "--concurrency", "11", "--input-file", "missing", "--output-dir", t.TempDir(), "--state-scope", "scope"}} {
		var out, errOut bytes.Buffer
		if code := run(args, strings.NewReader(""), &out, &errOut); code == 0 || out.Len() != 0 || errOut.Len() == 0 {
			t.Fatalf("invalid args accepted: %v", args)
		}
	}
	var out bytes.Buffer
	if code := run([]string{"batch", "--help"}, strings.NewReader(""), &out, io.Discard); code != 0 || !strings.Contains(out.String(), "--concurrency") {
		t.Fatal("missing batch help")
	}
}
