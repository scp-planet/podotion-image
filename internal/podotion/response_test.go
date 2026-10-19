package podotion

import (
	"context"
	"encoding/base64"
	"encoding/binary"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func fakePNG(width, height int) []byte {
	data := make([]byte, 33)
	copy(data, []byte{0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a})
	binary.BigEndian.PutUint32(data[16:20], uint32(width))
	binary.BigEndian.PutUint32(data[20:24], uint32(height))
	return data
}

func TestExtractImageResults(t *testing.T) {
	encoded := base64.StdEncoding.EncodeToString(fakePNG(1024, 1024))
	tests := []struct {
		name    string
		payload map[string]any
		kind    string
		source  string
		ok      bool
	}{
		{
			name:    "data base64",
			payload: map[string]any{"data": []any{map[string]any{"b64_json": encoded}}},
			kind:    "base64",
			source:  "$.data[0].b64_json",
			ok:      true,
		},
		{
			name:    "data url",
			payload: map[string]any{"data": []any{map[string]any{"url": "https://example.com/image.png"}}},
			kind:    "url",
			source:  "$.data[0].url",
			ok:      true,
		},
		{
			name: "output completed",
			payload: map[string]any{"output": []any{map[string]any{
				"type":   "image_generation_call",
				"status": "completed",
				"result": encoded,
			}}},
			kind:   "base64",
			source: "$.output[0].result",
			ok:     true,
		},
		{
			name: "response output",
			payload: map[string]any{"response": map[string]any{"output": []any{map[string]any{
				"type":     "image_generation_call",
				"b64_json": encoded,
			}}}},
			kind:   "base64",
			source: "$.response.output[0].b64_json",
			ok:     true,
		},
		{
			name:    "partial ignored",
			payload: map[string]any{"output": []any{map[string]any{"type": "image_generation_call", "status": "completed", "partial_image": "x"}}},
			ok:      false,
		},
		{
			name:    "multiple images",
			payload: map[string]any{"data": []any{map[string]any{"b64_json": encoded}, map[string]any{"b64_json": encoded}}},
			ok:      false,
		},
		{
			name:    "no image",
			payload: map[string]any{"created": 1},
			ok:      false,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			results, err := ExtractImageResults(test.payload)
			if !test.ok {
				if err == nil {
					t.Fatalf("expected an error")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if len(results) != 1 || results[0].Kind != test.kind || results[0].Source != test.source {
				t.Fatalf("unexpected results: %+v", results)
			}
		})
	}
}

func TestDecodeImageResultBase64AndDataURL(t *testing.T) {
	raw := fakePNG(640, 480)
	encoded := base64.StdEncoding.EncodeToString(raw)
	for _, result := range []ImageResult{
		{Kind: "base64", Value: encoded},
		{Kind: "data_url", Value: "data:image/png;base64," + encoded},
	} {
		data, mime, suffix, err := DecodeImageResult(context.Background(), result, time.Second)
		if err != nil {
			t.Fatal(err)
		}
		if string(data) != string(raw) || mime != "image/png" || suffix != ".png" {
			t.Fatalf("unexpected decode result: mime=%q suffix=%q bytes=%d", mime, suffix, len(data))
		}
	}
}

func TestDecodeImageResultDownloadsURL(t *testing.T) {
	raw := fakePNG(800, 600)
	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("Content-Type", "image/png")
		_, _ = writer.Write(raw)
	}))
	defer server.Close()
	originalTransport := providerTransport
	originalValidate := validateDownloadURL
	providerTransport = server.Client().Transport
	validateDownloadURL = func(string) error { return nil }
	defer func() {
		providerTransport = originalTransport
		validateDownloadURL = originalValidate
	}()
	data, mime, _, err := DecodeImageResult(context.Background(), ImageResult{Kind: "url", Value: server.URL + "/image.png"}, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != string(raw) || mime != "image/png" {
		t.Fatalf("unexpected downloaded image")
	}
}

func TestSaveImageResultsWritesAtomically(t *testing.T) {
	raw := fakePNG(1024, 768)
	encoded := base64.StdEncoding.EncodeToString(raw)
	output := t.TempDir()
	images, warnings, err := SaveImageResults(context.Background(), []ImageResult{{Kind: "base64", Value: encoded, Source: "$.data[0].b64_json"}}, output, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if len(warnings) != 0 || len(images) != 1 {
		t.Fatalf("unexpected save result")
	}
	if _, err := os.Stat(images[0].Path); err != nil {
		t.Fatal(err)
	}
	if images[0].Width == nil || *images[0].Width != 1024 || images[0].Height == nil || *images[0].Height != 768 {
		t.Fatalf("dimensions were not read")
	}
	if !strings.HasSuffix(images[0].Path, ".png") {
		t.Fatalf("unexpected suffix: %s", images[0].Path)
	}
}

func TestImageSizeWarnings(t *testing.T) {
	width, height := 1024, 1024
	images := []SavedImage{{Width: &width, Height: &height}}
	if warnings := ImageSizeWarnings("1024x1024", images); len(warnings) != 0 {
		t.Fatalf("unexpected warning")
	}
	if warnings := ImageSizeWarnings("1280x720", images); len(warnings) != 1 {
		t.Fatalf("expected one warning, got %d", len(warnings))
	}
	if warnings := ImageSizeWarnings(AutoSize, images); len(warnings) != 0 {
		t.Fatalf("auto size should not warn")
	}
}

func TestSaveImageResultsRejectsMultiple(t *testing.T) {
	_, _, err := SaveImageResults(context.Background(), []ImageResult{{Kind: "base64", Value: "x"}, {Kind: "base64", Value: "y"}}, filepath.Join(t.TempDir(), "out"), time.Second)
	if err == nil {
		t.Fatal("expected an error")
	}
}
