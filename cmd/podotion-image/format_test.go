package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestCLIOutputFormatPreflightAndHelp(t *testing.T) {
	for _, operation := range []string{"generate", "edit"} {
		out, stderr, code := runCLI(t, nil, "", operation, "--help")
		if code != 0 || stderr != "" || !strings.Contains(out, "--output-format") {
			t.Fatalf("missing format help: %s %s", out, stderr)
		}
	}
	dir := t.TempDir()
	output := filepath.Join(dir, "output")
	configDir := filepath.Join(dir, "podotion-image")
	if err := os.MkdirAll(configDir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(configDir, "provider.toml"), []byte("base_url = \"https://ai.podotion.com/v1\"\nPodotionImageSk = \"sk-test\"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	args := []string{"edit", "--prompt", "change animation colors, preserve motion", "--request-key", "gif-edit-input-validation", "--output-format", "GIF", "--image", filepath.Join(dir, "missing.gif"), "--output-dir", output}
	out, stderr, code := runCLI(t, []string{"CODEX_HOME=" + dir, "HTTPS_PROXY=http://127.0.0.1:1"}, "", args...)
	var payload struct {
		Error struct {
			Kind     string         `json:"error_kind"`
			Message  string         `json:"message"`
			Attempts int            `json:"attempts"`
			Details  map[string]any `json:"details"`
		} `json:"error"`
	}
	if json.Unmarshal([]byte(stderr), &payload) != nil || code != 1 || out != "" || payload.Error.Kind == "unsupported_edit_method" || !strings.Contains(payload.Error.Message, "image file not found") {
		t.Fatalf("GIF edit was rejected for the wrong reason: %d %s %s", code, out, stderr)
	}
	if _, err := os.Stat(output); !os.IsNotExist(err) {
		t.Fatal("invalid command wrote request state")
	}
	out, stderr, code = runCLI(t, []string{"CODEX_HOME=" + dir}, "", "generate", "--prompt", "test", "--request-key", "invalid-format-key", "--output-format", "svg")
	if code != 1 || out != "" || !strings.Contains(stderr, "output format must be") {
		t.Fatalf("invalid output accepted: %s %s", out, stderr)
	}
}
