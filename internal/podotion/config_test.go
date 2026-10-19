package podotion

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeTestConfig(t *testing.T, content string) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "provider.toml")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestLoadConfigDefaultsModelForLegacyFile(t *testing.T) {
	path := writeTestConfig(t, "base_url = \"https://ai.podotion.com/v1\"\nPodotionImageSk = \"sk-default\"\n")
	config, err := LoadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	if config.Model != LegacyDefaultModel {
		t.Fatalf("model = %q, want %q", config.Model, LegacyDefaultModel)
	}
	if config.BearerToken != "sk-default" {
		t.Fatalf("default credential was not loaded")
	}
	if config.BearerToken4K != "" {
		t.Fatalf("unexpected 4K credential")
	}
}

func TestLoadConfigSupportsInlineComments(t *testing.T) {
	path := writeTestConfig(t, "base_url = \"https://ai.podotion.com/v1\" # fixed\nmodel = 'vendor/image-3' # literal\nPodotionImageSk = \"sk-#-value\" # secret\n")
	config, err := LoadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	if config.Model != "vendor/image-3" || config.BearerToken != "sk-#-value" {
		t.Fatalf("unexpected config: %+v", config)
	}
}

func TestLoadConfigUsesConfiguredModel(t *testing.T) {
	path := writeTestConfig(t, "base_url = \"https://ai.podotion.com/v1\"\nmodel = \"vendor/image-3\"\nPodotionImageSk = \"sk-default\"\nPodotionImage4kSk = \"sk-4k\"\n")
	config, err := LoadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	if config.Model != "vendor/image-3" {
		t.Fatalf("model = %q", config.Model)
	}
	if config.BearerToken4K != "sk-4k" {
		t.Fatalf("4K credential was not loaded")
	}
}

func TestModelPrecedence(t *testing.T) {
	if got := ResolveModel("explicit", ModelSource{Model: "configured"}); got != "explicit" {
		t.Fatalf("explicit model precedence failed: %q", got)
	}
	if got := ResolveModel("", ModelSource{Model: "configured"}); got != "configured" {
		t.Fatalf("configured model precedence failed: %q", got)
	}
	if got := ResolveModel("", ModelSource{}); got != DefaultModel {
		t.Fatalf("default model precedence failed: %q", got)
	}
}

func TestValidateModel(t *testing.T) {
	valid := []string{"gpt-image-2", "gpt-image-2.5", "vendor/image-3", "team:image_4", "a"}
	for _, value := range valid {
		if _, err := ValidateModel(value); err != nil {
			t.Fatalf("ValidateModel(%q): %v", value, err)
		}
	}
	invalid := []string{"", "bad model", "a/b/c?", strings.Repeat("a", 129)}
	for _, value := range invalid {
		if _, err := ValidateModel(value); err == nil {
			t.Fatalf("ValidateModel(%q) unexpectedly succeeded", value)
		}
	}
}

func TestSetModelPreservesCredentials(t *testing.T) {
	path := writeTestConfig(t, "base_url = \"https://ai.podotion.com/v1\"\nPodotionImageSk = \"sk-default\"\nPodotionImage4kSk = \"sk-4k\"\n")
	if _, err := SetModel(path, "vendor/image-3"); err != nil {
		t.Fatal(err)
	}
	config, err := LoadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	if config.Model != "vendor/image-3" || config.BearerToken != "sk-default" || config.BearerToken4K != "sk-4k" {
		t.Fatalf("SetModel did not preserve credentials and model: %+v", config)
	}
	raw, _ := os.ReadFile(path)
	if !strings.Contains(string(raw), "model = \"vendor/image-3\"") {
		t.Fatalf("model was not written")
	}
}

func TestParseCredentialInput(t *testing.T) {
	tests := []struct {
		name    string
		input   string
		set4k   bool
		wantDef string
		want4k  string
		ok      bool
	}{
		{"assignments", "PodotionImageSk=sk-one\nPodotionImage4kSk=sk-four\n", false, "sk-one", "sk-four", true},
		{"raw lines", "sk-one\nsk-four\n", false, "sk-one", "sk-four", true},
		{"raw default", "sk-one\n", false, "sk-one", "", true},
		{"four k only", "PodotionImage4kSk=sk-four\n", true, "", "sk-four", true},
		{"four k raw", "sk-four\n", true, "", "sk-four", true},
		{"unknown key", "OtherKey=value\n", false, "", "", false},
		{"placeholder", "PodotionImageSk=__PODOTION_IMAGE_SK__\n", false, "", "", false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			def, four, err := ParseCredentialInput(test.input, test.set4k)
			if !test.ok {
				if err == nil {
					t.Fatalf("expected an error")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if def != test.wantDef || four != test.want4k {
				t.Fatalf("got default=%q 4k=%q, want default=%q 4k=%q", def, four, test.wantDef, test.want4k)
			}
		})
	}
}

func TestConfigCheckReportsModel(t *testing.T) {
	path := writeTestConfig(t, "base_url = \"https://ai.podotion.com/v1\"\nmodel = \"vendor/image-3\"\nPodotionImageSk = \"sk-default\"\n")
	result, err := ConfigCheck(path)
	if err != nil {
		t.Fatal(err)
	}
	if result["model"] != "vendor/image-3" {
		t.Fatalf("model = %v", result["model"])
	}
}
