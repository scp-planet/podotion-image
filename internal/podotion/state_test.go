package podotion

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestRequestFingerprintIncludesModel(t *testing.T) {
	dir := t.TempDir()
	first, _, _, err := RequestFingerprint("generate", "https://example.com/v1/images/generations", "gpt-image-2", "1024x1024", "default", dir, "scope", "prompt", nil)
	if err != nil {
		t.Fatal(err)
	}
	second, _, _, err := RequestFingerprint("generate", "https://example.com/v1/images/generations", "vendor/image-3", "1024x1024", "default", dir, "scope", "prompt", nil)
	if err != nil {
		t.Fatal(err)
	}
	if first == second {
		t.Fatal("model change must change the fingerprint")
	}
	third, _, _, err := RequestFingerprint("generate", "https://example.com/v1/images/generations", "gpt-image-2", "1024x1024", "default", strings.ToUpper(dir), "scope", "prompt", nil)
	if err != nil {
		t.Fatal(err)
	}
	if first != third {
		t.Fatal("output directory casing should be normalized on Windows")
	}
}

func TestRunGenerationReuseAndModelSwitch(t *testing.T) {
	var calls int32
	encoded := base64.StdEncoding.EncodeToString(fakePNG(1024, 1024))
	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		atomic.AddInt32(&calls, 1)
		var payload map[string]any
		_ = json.NewDecoder(request.Body).Decode(&payload)
		if payload["size"] != "1024x1024" {
			t.Errorf("unexpected size: %v", payload["size"])
		}
		_ = json.NewEncoder(writer).Encode(map[string]any{
			"data": []any{map[string]any{"b64_json": encoded}},
		})
	}))
	defer server.Close()
	originalTransport := providerTransport
	originalLoader := loadConfig
	providerTransport = server.Client().Transport
	loadConfig = func(string) (ProviderConfig, error) {
		return ProviderConfig{
			ProviderID:     DirectProviderID,
			Name:           SiteName,
			BaseURL:        server.URL,
			Model:          "configured-model",
			BearerToken:    "sk-secret",
			CredentialMode: "podotion_image_sk",
		}, nil
	}
	defer func() {
		providerTransport = originalTransport
		loadConfig = originalLoader
	}()

	output := t.TempDir()
	options := GenerateOptions{
		Operation:  "generate",
		Prompt:     "a test prompt",
		Size:       "1k",
		Ratio:      "1:1",
		OutputDir:  output,
		RequestKey: "request-key-0001",
		StateScope: "scope",
	}
	result, err := RunGeneration(context.Background(), options)
	if err != nil {
		t.Fatal(err)
	}
	if result["model"] != "configured-model" {
		t.Fatalf("unexpected model: %v", result["model"])
	}
	request := result["request"].(map[string]any)
	if request["reused"] != false {
		t.Fatalf("first call should not be reused")
	}
	if atomic.LoadInt32(&calls) != 1 {
		t.Fatalf("expected one upstream call")
	}
	images := result["images"].([]map[string]any)
	if len(images) != 1 {
		t.Fatalf("expected one image")
	}
	imagePath := images[0]["path"].(string)
	if _, err := os.Stat(imagePath); err != nil {
		t.Fatal(err)
	}

	reused, err := RunGeneration(context.Background(), options)
	if err != nil {
		t.Fatal(err)
	}
	if reused["request"].(map[string]any)["reused"] != true {
		t.Fatalf("same request key should reuse the result")
	}
	if atomic.LoadInt32(&calls) != 1 {
		t.Fatalf("reuse must not call the provider")
	}

	aliasOptions := options
	aliasOptions.RequestKey = "request-key-0002"
	alias, err := RunGeneration(context.Background(), aliasOptions)
	if err != nil {
		t.Fatal(err)
	}
	if alias["request"].(map[string]any)["reused"] != true {
		t.Fatalf("equivalent recent request should be reused")
	}
	if atomic.LoadInt32(&calls) != 1 {
		t.Fatalf("alias reuse must not call the provider")
	}

	modelOverride := aliasOptions
	modelOverride.RequestKey = "request-key-0003"
	modelOverride.Model = "call-model"
	overrideResult, err := RunGeneration(context.Background(), modelOverride)
	if err != nil {
		t.Fatal(err)
	}
	if overrideResult["model"] != "call-model" {
		t.Fatalf("per-call model was not applied: %v", overrideResult["model"])
	}
	if atomic.LoadInt32(&calls) != 2 {
		t.Fatalf("model switch must not reuse the previous result")
	}

	forceOptions := modelOverride
	forceOptions.RequestKey = "request-key-0004"
	forceOptions.ForceNew = true
	time.Sleep(2 * time.Millisecond)
	if _, err := RunGeneration(context.Background(), forceOptions); err != nil {
		t.Fatal(err)
	}
	if atomic.LoadInt32(&calls) != 3 {
		t.Fatalf("force-new must call the provider")
	}
}

func TestRequestStatusAndAbandon(t *testing.T) {
	output := t.TempDir()
	scope := "scope"
	key := "request-key-abandon"
	record := &RequestRecord{
		Version:            RequestRecordVersion,
		RequestKey:         key,
		Fingerprint:        strings.Repeat("a", 64),
		FingerprintVersion: 1,
		Operation:          "generate",
		Model:              DefaultModel,
		Endpoint:           "https://example.com/v1/images/generations",
		Size:               "1024x1024",
		CredentialProfile:  "default",
		OutputDir:          output,
		StateScope:         scope,
		Status:             "outcome_unknown",
		MayHaveBeenBilled:  true,
		CreatedAt:          nowISO(),
		UpdatedAt:          nowISO(),
	}
	path, err := requestRecordPath(output, key, scope)
	if err != nil {
		t.Fatal(err)
	}
	if err := atomicWriteJSON(path, record); err != nil {
		t.Fatal(err)
	}
	status, err := GetRequestStatus(output, key, scope)
	if err != nil {
		t.Fatal(err)
	}
	if status["effective_status"] != "outcome_unknown" || status["safe_to_retry"] != false {
		t.Fatalf("unexpected status: %v", status)
	}
	if _, err := AbandonRequest(output, key, scope, false); err == nil {
		t.Fatal("abandon must require acknowledgement")
	}
	abandoned, err := AbandonRequest(output, key, scope, true)
	if err != nil {
		t.Fatal(err)
	}
	if abandoned["status"] != "abandoned" {
		t.Fatalf("unexpected abandon result: %v", abandoned)
	}
}

func TestReadLastImage(t *testing.T) {
	output := t.TempDir()
	path := filepath.Join(output, "image.png")
	if err := os.WriteFile(path, fakePNG(64, 64), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := WriteLastState(output, []SavedImage{NewSavedImage(path, "image/png", 33, nil, nil)}, "generate", DefaultModel, "1024x1024", "scope"); err != nil {
		t.Fatal(err)
	}
	last, err := ReadLastImage(output, "scope")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(last, "image.png") {
		t.Fatalf("unexpected last image: %s", last)
	}
}
