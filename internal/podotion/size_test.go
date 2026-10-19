package podotion

import "testing"

func TestResolveSizeTable(t *testing.T) {
	tests := []struct {
		tier   string
		ratio  string
		width  int
		height int
	}{
		{"1k", "1:1", 1024, 1024},
		{"1k", "16:9", 1280, 720},
		{"1k", "9:16", 720, 1280},
		{"2k", "1:1", 2048, 2048},
		{"2k", "16:9", 2048, 1152},
		{"4k", "1:1", 2880, 2880},
		{"4k", "16:9", 3840, 2160},
		{"4k", "9:16", 2160, 3840},
		{"4k", "19:10", 3648, 1920},
	}
	for _, test := range tests {
		resolved, err := ResolveSize(test.tier, test.ratio)
		if err != nil {
			t.Fatalf("ResolveSize(%q, %q): %v", test.tier, test.ratio, err)
		}
		if resolved.Width != test.width || resolved.Height != test.height {
			t.Fatalf("ResolveSize(%q, %q) = %dx%d, want %dx%d", test.tier, test.ratio, resolved.Width, resolved.Height, test.width, test.height)
		}
		if err := ValidateDimensions(resolved.Width, resolved.Height); err != nil {
			t.Fatalf("resolved dimensions are invalid: %v", err)
		}
	}
}

func TestResolveRequestSize(t *testing.T) {
	tests := []struct {
		size  string
		ratio string
		value string
		ok    bool
	}{
		{"", "", "auto", true},
		{"auto", "", "auto", true},
		{"1k", "1:1", "1024x1024", true},
		{"4k", "16:9", "3840x2160", true},
		{"2560x1440", "", "2560x1440", true},
		{"2560x1440", "16:9", "", false},
		{"1k", "", "", false},
		{"1k", "19:6", "", false},
		{"4096x2160", "", "", false},
		{"1000x1000", "", "", false},
	}
	for _, test := range tests {
		resolved, err := ResolveRequestSize(test.size, test.ratio)
		if test.ok && err != nil {
			t.Fatalf("ResolveRequestSize(%q, %q): %v", test.size, test.ratio, err)
		}
		if !test.ok {
			if err == nil {
				t.Fatalf("ResolveRequestSize(%q, %q) unexpectedly succeeded", test.size, test.ratio)
			}
			continue
		}
		if resolved.Value != test.value {
			t.Fatalf("ResolveRequestSize(%q, %q) = %q, want %q", test.size, test.ratio, resolved.Value, test.value)
		}
	}
}

func TestCredentialProfileSelection(t *testing.T) {
	tests := []struct {
		size  string
		ratio string
		want  string
	}{
		{"auto", "", "default"},
		{"1k", "1:1", "default"},
		{"2k", "16:9", "default"},
		{"4k", "1:1", "4k"},
		{"3840x2160", "", "4k"},
		{"2880x2880", "", "4k"},
		{"8294400x1", "", "4k"},
	}
	for _, test := range tests {
		resolved, err := ResolveRequestSize(test.size, test.ratio)
		if err != nil {
			if test.want == "4k" && test.size == "8294400x1" {
				continue
			}
			t.Fatalf("ResolveRequestSize(%q, %q): %v", test.size, test.ratio, err)
		}
		if got := resolved.CredentialProfile(); got != test.want {
			t.Fatalf("CredentialProfile(%q, %q) = %q, want %q", test.size, test.ratio, got, test.want)
		}
	}
}
