package contract_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/scp-planet/podotion-image/internal/podotion"
)

func skillRoot(t *testing.T) string {
	t.Helper()
	working, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	return filepath.Join(working, "..", "..", "skills", "podotion-image")
}

func TestProductionSkillHasNoScriptRuntime(t *testing.T) {
	root := skillRoot(t)
	err := filepath.WalkDir(root, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() && entry.Name() == "scripts" {
			t.Fatalf("production Skill must not contain a scripts directory")
		}
		if !entry.IsDir() && strings.HasSuffix(entry.Name(), ".py") {
			t.Fatalf("production Skill must not contain Python files")
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	skill, err := os.ReadFile(filepath.Join(root, "SKILL.md"))
	if err != nil {
		t.Fatal(err)
	}
	text := strings.ToLower(string(skill))
	for _, required := range []string{"podotion-image.cmd", "podotion-image.sh", "config set-model", "--model"} {
		if !strings.Contains(text, required) {
			t.Fatalf("SKILL.md is missing %q", required)
		}
	}
	for _, forbidden := range []string{"python3", "py -3", "scripts/podotion_image.py", "manage.py"} {
		if strings.Contains(text, forbidden) {
			t.Fatalf("SKILL.md still references %q", forbidden)
		}
	}
}

func TestLauncherAndManifestContract(t *testing.T) {
	root := skillRoot(t)
	for _, relative := range []string{
		"bin/podotion-image.cmd",
		"bin/podotion-image.ps1",
		"bin/podotion-image.sh",
		"agents/openai.yaml",
		"runtime.json",
	} {
		if _, err := os.Stat(filepath.Join(root, filepath.FromSlash(relative))); err != nil {
			t.Fatalf("missing %s: %v", relative, err)
		}
	}
	raw, err := os.ReadFile(filepath.Join(root, "runtime.json"))
	if err != nil {
		t.Fatal(err)
	}
	var manifest struct {
		Schema  int               `json:"schema"`
		Version string            `json:"version"`
		Tag     string            `json:"tag"`
		Assets  map[string]string `json:"assets"`
	}
	if err := json.Unmarshal(raw, &manifest); err != nil {
		t.Fatal(err)
	}
	if manifest.Schema != 1 || len(manifest.Assets) != 6 {
		t.Fatalf("unexpected runtime manifest: %+v", manifest)
	}
	if manifest.Version != podotion.BinaryVersion {
		t.Fatalf("runtime version %q does not match binary version %q", manifest.Version, podotion.BinaryVersion)
	}
	if manifest.Tag != "v"+manifest.Version {
		t.Fatalf("runtime tag %q does not match version %q", manifest.Tag, manifest.Version)
	}
	for _, key := range []string{"windows-amd64", "windows-arm64", "darwin-amd64", "darwin-arm64", "linux-amd64", "linux-arm64"} {
		if manifest.Assets[key] == "" {
			t.Fatalf("runtime manifest is missing %s", key)
		}
	}
	agent, err := os.ReadFile(filepath.Join(root, "agents", "openai.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(agent), "allow_implicit_invocation: true") {
		t.Fatalf("implicit invocation is not enabled")
	}
	if !strings.Contains(string(agent), "v"+manifest.Version) {
		t.Fatalf("Skill description does not contain runtime version v%s", manifest.Version)
	}
}
