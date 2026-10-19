package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync/atomic"
	"testing"
)

var testBinary string

func TestMain(m *testing.M) {
	working, err := os.Getwd()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	root := filepath.Dir(filepath.Dir(working))
	temp, err := os.MkdirTemp("", "podotion-image-tests-*")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	testBinary = filepath.Join(temp, "podotion-image")
	if runtime.GOOS == "windows" {
		testBinary += ".exe"
	}
	command := exec.Command("go", "build", "-trimpath", "-o", testBinary, "./cmd/podotion-image")
	command.Dir = root
	command.Env = testEnvironment()
	command.Stdout = os.Stderr
	command.Stderr = os.Stderr
	if err := command.Run(); err != nil {
		fmt.Fprintln(os.Stderr, "failed to build test binary:", err)
		os.RemoveAll(temp)
		os.Exit(1)
	}
	code := m.Run()
	os.RemoveAll(temp)
	os.Exit(code)
}

func testEnvironment() []string {
	env := os.Environ()
	if os.Getenv("GOCACHE") == "" {
		env = append(env, "GOCACHE="+filepath.Join(os.TempDir(), "podotion-image-go-cache"))
	}
	if os.Getenv("GOMODCACHE") == "" {
		env = append(env, "GOMODCACHE="+filepath.Join(os.TempDir(), "podotion-image-go-mod-cache"))
	}
	return env
}

func runCLI(t *testing.T, env []string, stdin string, args ...string) (string, string, int) {
	t.Helper()
	command := exec.Command(testBinary, args...)
	command.Env = append(testEnvironment(), env...)
	if stdin != "" {
		command.Stdin = strings.NewReader(stdin)
	}
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	command.Stdout = &stdout
	command.Stderr = &stderr
	err := command.Run()
	code := 0
	if err != nil {
		var exitErr *exec.ExitError
		if !strings.Contains(err.Error(), "exit status") {
			t.Fatalf("failed to run CLI: %v", err)
		}
		exitErr = err.(*exec.ExitError)
		code = exitErr.ExitCode()
	}
	return stdout.String(), stderr.String(), code
}

func TestCLIVersionAndSizes(t *testing.T) {
	stdout, stderr, code := runCLI(t, nil, "", "version")
	if code != 0 {
		t.Fatalf("version failed: %s", stderr)
	}
	var payload map[string]any
	if err := json.Unmarshal([]byte(stdout), &payload); err != nil {
		t.Fatal(err)
	}
	if payload["version"] != "2.2.0" || payload["default_model"] != "gpt-image-2.5" {
		t.Fatalf("unexpected version payload: %v", payload)
	}
	stdout, stderr, code = runCLI(t, nil, "", "sizes")
	if code != 0 {
		t.Fatalf("sizes failed: %s", stderr)
	}
	if err := json.Unmarshal([]byte(stdout), &payload); err != nil {
		t.Fatal(err)
	}
	if payload["sizes"].(map[string]any)["4k"].(map[string]any)["16:9"] != "3840x2160" {
		t.Fatalf("unexpected size mapping")
	}
}

func TestCLIConfigAndModel(t *testing.T) {
	codexHome := t.TempDir()
	env := []string{"CODEX_HOME=" + codexHome}
	stdout, stderr, code := runCLI(t, env, "PodotionImageSk=sk-default\nPodotionImage4kSk=sk-4k\n", "config", "--stdin", "--force")
	if code != 0 {
		t.Fatalf("config failed: %s", stderr)
	}
	var payload map[string]any
	if err := json.Unmarshal([]byte(stdout), &payload); err != nil {
		t.Fatal(err)
	}
	if payload["model"] != "gpt-image-2.5" {
		t.Fatalf("unexpected default model: %v", payload["model"])
	}
	_, stderr, code = runCLI(t, env, "", "config", "set-model", "vendor/image-3")
	if code != 0 {
		t.Fatalf("set-model failed: %s", stderr)
	}
	stdout, stderr, code = runCLI(t, env, "", "config", "--check")
	if code != 0 {
		t.Fatalf("config check failed: %s", stderr)
	}
	if err := json.Unmarshal([]byte(stdout), &payload); err != nil {
		t.Fatal(err)
	}
	if payload["model"] != "vendor/image-3" {
		t.Fatalf("model was not persisted: %v", payload["model"])
	}
	raw, err := os.ReadFile(filepath.Join(codexHome, "podotion-image", "provider.toml"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(raw), `model = "vendor/image-3"`) {
		t.Fatalf("provider.toml does not contain the model")
	}
}

func TestCLIConfigPreservesExistingModel(t *testing.T) {
	tests := []struct {
		name    string
		content string
		want    string
	}{
		{
			name:    "explicit model",
			content: "base_url = \"https://ai.podotion.com/v1\"\nmodel = \"gpt-image-2\"\nPodotionImageSk = \"sk-old\"\n",
			want:    "gpt-image-2",
		},
		{
			name:    "legacy config without model",
			content: "base_url = \"https://ai.podotion.com/v1\"\nPodotionImageSk = \"sk-old\"\n",
			want:    "gpt-image-2",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			codexHome := t.TempDir()
			configDir := filepath.Join(codexHome, "podotion-image")
			if err := os.MkdirAll(configDir, 0o700); err != nil {
				t.Fatal(err)
			}
			configPath := filepath.Join(configDir, "provider.toml")
			if err := os.WriteFile(configPath, []byte(test.content), 0o600); err != nil {
				t.Fatal(err)
			}
			stdout, stderr, code := runCLI(t, []string{"CODEX_HOME=" + codexHome}, "PodotionImageSk=sk-new\n", "config", "--stdin", "--force")
			if code != 0 {
				t.Fatalf("config failed: %s", stderr)
			}
			var payload map[string]any
			if err := json.Unmarshal([]byte(stdout), &payload); err != nil {
				t.Fatal(err)
			}
			if payload["model"] != test.want {
				t.Fatalf("existing model changed: got %v, want %s", payload["model"], test.want)
			}
			raw, err := os.ReadFile(configPath)
			if err != nil {
				t.Fatal(err)
			}
			if !strings.Contains(string(raw), `model = "`+test.want+`"`) {
				t.Fatalf("provider.toml does not preserve model %s: %s", test.want, raw)
			}
			if !strings.Contains(string(raw), `PodotionImageSk = "sk-new"`) {
				t.Fatalf("provider.toml does not contain the new credential")
			}
		})
	}
}
func TestCLIGenerateWithoutCredentialsFailsSafely(t *testing.T) {
	codexHome := t.TempDir()
	output := t.TempDir()
	stdout, stderr, code := runCLI(t, []string{"CODEX_HOME=" + codexHome}, "", "generate", "--prompt", "test", "--request-key", "request-key-0001", "--output-dir", output)
	if code == 0 {
		t.Fatalf("generate unexpectedly succeeded: %s", stdout)
	}
	if stdout != "" {
		t.Fatalf("unexpected stdout: %s", stdout)
	}
	var payload map[string]any
	if err := json.Unmarshal([]byte(stderr), &payload); err != nil {
		t.Fatal(err)
	}
	if payload["ok"] != false {
		t.Fatalf("error payload is missing ok:false")
	}
	errorValue := payload["error"].(map[string]any)
	if !strings.Contains(errorValue["message"].(string), "credential file not found") {
		t.Fatalf("unexpected error: %v", errorValue)
	}
}

func TestCLIRequestStatusNotFound(t *testing.T) {
	output := t.TempDir()
	stdout, stderr, code := runCLI(t, nil, "", "request-status", "--request-key", "request-key-0001", "--output-dir", output)
	if code != 0 {
		t.Fatalf("request-status failed: %s", stderr)
	}
	var payload map[string]any
	if err := json.Unmarshal([]byte(stdout), &payload); err != nil {
		t.Fatal(err)
	}
	if payload["found"] != false {
		t.Fatalf("unexpected status: %v", payload)
	}
}

func TestLauncherBootstrapChecksumAndCache(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping launcher bootstrap in short mode")
	}
	working, _ := os.Getwd()
	root := filepath.Dir(filepath.Dir(working))
	sourceBin := filepath.Join(root, "skills", "podotion-image", "bin")
	var requests int32
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		atomic.AddInt32(&requests, 1)
		if strings.HasSuffix(request.URL.Path, ".sha256") {
			digest, err := fileSHA256(testBinary)
			if err != nil {
				http.Error(writer, "hash failed", 500)
				return
			}
			_, _ = writer.Write([]byte(digest + "  asset\n"))
			return
		}
		data, err := os.ReadFile(testBinary)
		if err != nil {
			http.Error(writer, "read failed", 500)
			return
		}
		_, _ = writer.Write(data)
	}))
	defer server.Close()

	skill := makeLauncherSkill(t, sourceBin, server.URL)
	codexHome := t.TempDir()
	env := []string{"CODEX_HOME=" + codexHome}
	output, errOutput, code := runLauncher(t, skill, env, "version")
	if code != 0 {
		t.Fatalf("launcher failed: %s", errOutput)
	}
	if !strings.Contains(output, `"version": "2.2.0"`) {
		t.Fatalf("unexpected launcher output: %s", output)
	}
	cacheBinary := launcherCachePath(codexHome)
	if _, err := os.Stat(cacheBinary); err != nil {
		t.Fatalf("cached binary was not created: %v", err)
	}
	firstRequests := atomic.LoadInt32(&requests)
	if firstRequests < 2 {
		t.Fatalf("launcher did not download binary and checksum")
	}
	output, errOutput, code = runLauncher(t, skill, env, "version")
	if code != 0 {
		t.Fatalf("cached launcher failed: %s", errOutput)
	}
	if atomic.LoadInt32(&requests) != firstRequests {
		t.Fatalf("cache hit performed a download")
	}

	badServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if strings.HasSuffix(request.URL.Path, ".sha256") {
			_, _ = writer.Write([]byte(strings.Repeat("0", 64) + "  asset\n"))
			return
		}
		data, _ := os.ReadFile(testBinary)
		_, _ = writer.Write(data)
	}))
	defer badServer.Close()
	badSkill := makeLauncherSkill(t, sourceBin, badServer.URL)
	badHome := t.TempDir()
	_, errOutput, code = runLauncher(t, badSkill, []string{"CODEX_HOME=" + badHome}, "version")
	if code == 0 {
		t.Fatalf("launcher accepted a bad checksum")
	}
	if !strings.Contains(strings.ToLower(errOutput), "checksum") {
		t.Fatalf("unexpected checksum error: %s", errOutput)
	}
	if _, err := os.Stat(launcherCachePath(badHome)); err == nil {
		t.Fatalf("bad binary was cached")
	}
}

func makeLauncherSkill(t *testing.T, sourceBin, baseURL string) string {
	t.Helper()
	root := t.TempDir()
	bin := filepath.Join(root, "bin")
	if err := os.MkdirAll(bin, 0o755); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"podotion-image.cmd", "podotion-image.ps1", "podotion-image.sh"} {
		data, err := os.ReadFile(filepath.Join(sourceBin, name))
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(bin, name), data, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	asset := fmt.Sprintf("podotion-image-%s-%s", runtime.GOOS, runtime.GOARCH)
	if runtime.GOOS == "windows" {
		asset += ".exe"
	}
	manifest := map[string]any{
		"schema":           1,
		"version":          "2.2.0",
		"repository":       "scp-planet/podotion-image",
		"tag":              "v2.2.0",
		"release_base_url": baseURL,
		"assets": map[string]string{
			runtime.GOOS + "-" + runtime.GOARCH: asset,
		},
	}
	raw, _ := json.Marshal(manifest)
	if err := os.WriteFile(filepath.Join(root, "runtime.json"), raw, 0o600); err != nil {
		t.Fatal(err)
	}
	return root
}

func runLauncher(t *testing.T, skill string, env []string, args ...string) (string, string, int) {
	t.Helper()
	var command *exec.Cmd
	if runtime.GOOS == "windows" {
		commandArgs := append([]string{"-NoProfile", "-ExecutionPolicy", "Bypass", "-File", filepath.Join(skill, "bin", "podotion-image.ps1")}, args...)
		command = exec.Command("powershell.exe", commandArgs...)
	} else {
		commandArgs := append([]string{filepath.Join(skill, "bin", "podotion-image.sh")}, args...)
		command = exec.Command("sh", commandArgs...)
	}
	command.Env = append(testEnvironment(), env...)
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	command.Stdout = &stdout
	command.Stderr = &stderr
	err := command.Run()
	code := 0
	if err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			code = exitErr.ExitCode()
		} else {
			t.Fatalf("launcher did not start: %v", err)
		}
	}
	return stdout.String(), stderr.String(), code
}

func launcherCachePath(codexHome string) string {
	name := "podotion-image"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	return filepath.Join(codexHome, "podotion-image", "runtime", "2.2.0", runtime.GOOS+"-"+runtime.GOARCH, name)
}

func fileSHA256(path string) (string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(data)
	return hex.EncodeToString(digest[:]), nil
}
