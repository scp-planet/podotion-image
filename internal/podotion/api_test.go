package podotion

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
)

func TestBuildImagesGenerationPayload(t *testing.T) {
	raw, err := BuildImagesGenerationPayload("vendor/image-3", "a prompt", "1024x1024")
	if err != nil {
		t.Fatal(err)
	}
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatal(err)
	}
	if payload["model"] != "vendor/image-3" || payload["prompt"] != "a prompt" || payload["size"] != "1024x1024" || payload["n"] != float64(1) {
		t.Fatalf("unexpected payload: %v", payload)
	}
}

func TestPostImagesGenerateUsesModelAndRedactsErrors(t *testing.T) {
	var received map[string]any
	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if !strings.HasSuffix(request.URL.Path, "/v1/images/generations") {
			t.Errorf("unexpected path: %s", request.URL.Path)
		}
		if request.Header.Get("Authorization") != "Bearer sk-secret" {
			t.Errorf("unexpected authorization header")
		}
		raw, _ := io.ReadAll(request.Body)
		_ = json.Unmarshal(raw, &received)
		_ = json.NewEncoder(writer).Encode(map[string]any{"data": []any{map[string]any{"b64_json": base64.StdEncoding.EncodeToString(fakePNG(64, 64))}}})
	}))
	defer server.Close()
	original := providerTransport
	providerTransport = server.Client().Transport
	defer func() { providerTransport = original }()
	provider := ProviderConfig{BaseURL: server.URL, Model: "vendor/image-3", BearerToken: "sk-secret", ProviderID: DirectProviderID, Name: SiteName}
	response, err := PostImages(context.Background(), provider, "generate", "prompt-text", nil, "1024x1024")
	if err != nil {
		t.Fatal(err)
	}
	if received["model"] != "vendor/image-3" {
		t.Fatalf("model was not sent: %v", received["model"])
	}
	if _, ok := response["data"]; !ok {
		t.Fatalf("unexpected response")
	}

	errorServer := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.WriteHeader(http.StatusBadRequest)
		_, _ = writer.Write([]byte(`{"error":{"message":"bad sk-secret prompt-text"}}`))
	}))
	defer errorServer.Close()
	providerTransport = errorServer.Client().Transport
	provider.BaseURL = errorServer.URL
	_, err = PostImages(context.Background(), provider, "generate", "prompt-text", nil, "1024x1024")
	if err == nil {
		t.Fatal("expected error")
	}
	message := err.Error()
	if strings.Contains(message, "sk-secret") || strings.Contains(message, "prompt-text") {
		t.Fatalf("error was not redacted: %s", message)
	}
}

func TestPostImagesEditMultipart(t *testing.T) {
	imagePath := t.TempDir() + "/input.png"
	if err := os.WriteFile(imagePath, encodedFixture(t, "png"), 0o600); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if !strings.HasSuffix(request.URL.Path, "/v1/images/edits") {
			t.Errorf("unexpected path: %s", request.URL.Path)
		}
		reader, err := request.MultipartReader()
		if err != nil {
			t.Errorf("multipart parse failed: %v", err)
			return
		}
		fields := map[string]string{}
		fileCount := 0
		for {
			part, err := reader.NextPart()
			if err == io.EOF {
				break
			}
			if err != nil {
				t.Errorf("part read failed: %v", err)
				return
			}
			data, _ := io.ReadAll(part)
			if strings.HasPrefix(part.FormName(), "image") {
				fileCount++
				if len(data) == 0 {
					t.Errorf("empty image part")
				}
			} else {
				fields[part.FormName()] = string(data)
			}
		}
		if fields["model"] != "vendor/image-3" || fields["size"] != "1024x1024" || fileCount != 1 {
			t.Errorf("unexpected multipart fields: %v files=%d", fields, fileCount)
		}
		_ = json.NewEncoder(writer).Encode(map[string]any{"data": []any{map[string]any{"b64_json": base64.StdEncoding.EncodeToString(fakePNG(64, 64))}}})
	}))
	defer server.Close()
	original := providerTransport
	providerTransport = server.Client().Transport
	defer func() { providerTransport = original }()
	provider := ProviderConfig{BaseURL: server.URL, Model: "vendor/image-3", BearerToken: "sk-secret"}
	if _, err := PostImages(context.Background(), provider, "edit", "edit it", []string{imagePath}, "1024x1024"); err != nil {
		t.Fatal(err)
	}
}

func TestPostImagesHonorsContextCancellation(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		time.Sleep(300 * time.Millisecond)
		_, _ = writer.Write([]byte(`{}`))
	}))
	defer server.Close()
	original := providerTransport
	providerTransport = server.Client().Transport
	defer func() { providerTransport = original }()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	provider := ProviderConfig{BaseURL: server.URL, Model: DefaultModel, BearerToken: "sk-secret"}
	if _, err := PostImages(ctx, provider, "generate", "prompt", nil, "auto"); err == nil {
		t.Fatal("expected cancellation error")
	}
}
