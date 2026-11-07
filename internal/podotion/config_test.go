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

func TestDefaultModelMigration(t *testing.T) {
	for _, test := range []struct {
		name, model, policy, want, wantPolicy string
		changed                               bool
	}{
		{"missing", "", "", DefaultModel, "default", true},
		{"old default", "gpt-image-2", "", DefaultModel, "default", true},
		{"previous default", "gpt-image-2.5", "", DefaultModel, "default", true},
		{"current default", DefaultModel, "", DefaultModel, "default", true},
		{"tracked default", "gpt-image-2", "default", DefaultModel, "default", true},
		{"custom legacy", "vendor/image-3", "", "vendor/image-3", "user", false},
		{"explicit old default", "gpt-image-2", "user", "gpt-image-2", "user", false},
		{"explicit previous default", "gpt-image-2.5", "user", "gpt-image-2.5", "user", false},
		{"migrated", DefaultModel, "default", DefaultModel, "default", false},
	} {
		t.Run(test.name, func(t *testing.T) {
			content := "\ufeff# keep this comment\r\nbase_url = \"https://ai.podotion.com/v1\"\r\nPodotionImageSk = 'sk-default' # untouched\r\nPodotionImage4kSk = \"sk-4k\"\r\n"
			if test.model != "" {
				content += "model = \"" + test.model + "\"\r\n"
			}
			if test.policy != "" {
				content += "model_policy = \"" + test.policy + "\"\r\n"
			}
			path := writeTestConfig(t, content)
			config, err := LoadConfig(path)
			if err != nil || config.Model != test.want || config.ModelPolicy != test.wantPolicy {
				t.Fatalf("resolution: %v", err)
			}
			preview, err := MigrateDefaultModel(path, true)
			if err != nil {
				t.Fatal(err)
			}
			before, _ := os.ReadFile(path)
			if string(before) != content || preview["updated"] != false {
				t.Fatal("dry run modified configuration")
			}
			result, err := MigrateDefaultModel(path, false)
			if err != nil || result["updated"] != test.changed {
				t.Fatalf("migration: %v %v", result, err)
			}
			after, _ := os.ReadFile(path)
			if !strings.Contains(string(after), "PodotionImageSk = 'sk-default' # untouched\r\nPodotionImage4kSk = \"sk-4k\"\r\n") || !strings.HasPrefix(string(after), "\ufeff# keep this comment\r\n") {
				t.Fatal("migration changed credentials or comments")
			}
			if !test.changed && string(after) != content {
				t.Fatal("custom configuration changed")
			}
			again, err := MigrateDefaultModel(path, false)
			if err != nil || again["updated"] != false {
				t.Fatal("migration was not idempotent")
			}
		})
	}
}

func TestExplicitModelPolicySurvivesCredentialUpdates(t *testing.T) {
	path := writeTestConfig(t, "base_url = \"https://ai.podotion.com/v1\"\nPodotionImageSk = \"sk-default\"\n")
	if _, err := SetModel(path, "gpt-image-2"); err != nil {
		t.Fatal(err)
	}
	if _, err := WriteConfig(path, "sk-new", "sk-4k", "gpt-image-2"); err != nil {
		t.Fatal(err)
	}
	if _, err := MigrateDefaultModel(path, false); err != nil {
		t.Fatal(err)
	}
	config, err := LoadConfig(path)
	if err != nil || config.Model != "gpt-image-2" || config.ModelPolicy != "user" || config.BearerToken != "sk-new" || config.BearerToken4K != "sk-4k" {
		t.Fatalf("explicit model or credentials changed: %v", err)
	}
}

func TestInvalidModelPolicy(t *testing.T) {
	for _, fields := range []string{"model_policy = \"invalid\"\n", "model_policy = \"user\"\n", "model = \"bad model\"\nmodel_policy = \"default\"\n"} {
		path := writeTestConfig(t, "base_url = \"https://ai.podotion.com/v1\"\nPodotionImageSk = \"sk-default\"\n"+fields)
		if _, err := LoadConfig(path); err == nil {
			t.Fatal("invalid policy accepted")
		}
		before, _ := os.ReadFile(path)
		if _, err := MigrateDefaultModel(path, false); err == nil {
			t.Fatal("invalid config migrated")
		}
		after, _ := os.ReadFile(path)
		if string(before) != string(after) {
			t.Fatal("invalid config modified")
		}
	}
}

func TestRequestModelPrecedence(t *testing.T) {
	tests := []struct {
		name, operation, format, explicit, configured, expected string
	}{
		{name: "gif uses gif model when no model is configured", operation: "generate", format: "gif", expected: GIFDefaultModel},
		{name: "gif is case insensitive", operation: "generate", format: "GIF", expected: GIFDefaultModel},
		{name: "explicit model wins", operation: "generate", format: "gif", explicit: "explicit-model", configured: "custom-model", expected: "explicit-model"},
		{name: "configured model wins", operation: "generate", format: "gif", configured: "custom-model", expected: "custom-model"},
		{name: "non gif uses configured model", operation: "generate", format: "png", configured: "custom-model", expected: "custom-model"},
		{name: "non gif uses normal default", operation: "generate", format: "png", expected: DefaultModel},
		{name: "gif edit uses normal default", operation: "edit", format: "gif", expected: DefaultModel},
		{name: "png edit uses configured model", operation: "edit", format: "png", configured: "custom-model", expected: "custom-model"},
		{name: "gif edit uses explicit model", operation: "edit", format: "gif", configured: "custom-model", explicit: "explicit-model", expected: "explicit-model"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if actual := ResolveRequestModel(test.operation, test.format, test.explicit, ModelSource{Model: test.configured}); actual != test.expected {
				t.Fatalf("ResolveRequestModel() = %q, want %q", actual, test.expected)
			}
		})
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
