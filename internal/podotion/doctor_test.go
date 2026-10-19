package podotion

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestRunDoctorWithoutProbe(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/v1/models" {
			t.Errorf("unexpected path: %s", request.URL.Path)
		}
		_, _ = writer.Write([]byte(`{"data":[]}`))
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
			BearerToken:    "sk-default",
			CredentialMode: "podotion_image_sk",
		}, nil
	}
	defer func() {
		providerTransport = originalTransport
		loadConfig = originalLoader
	}()
	result, err := RunDoctor(context.Background(), DoctorOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if result["ok"] != true || result["model"] != "configured-model" {
		t.Fatalf("unexpected doctor result: %v", result)
	}
	profiles := result["credential_profiles"].(map[string]any)
	fourK := profiles["4k"].(map[string]any)
	if fourK["configured"] != false {
		t.Fatalf("4K should not be configured")
	}
	warnings := result["warnings"].([]map[string]any)
	if len(warnings) != 1 || warnings[0]["code"] != "optional_4k_credential_missing" {
		t.Fatalf("unexpected warnings: %v", warnings)
	}
}

func TestRunDoctorProbeUsesModel(t *testing.T) {
	encoded := base64.StdEncoding.EncodeToString(fakePNG(1024, 1024))
	var probeModel string
	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/v1/models":
			_, _ = writer.Write([]byte(`{"data":[]}`))
		case "/v1/images/generations":
			var payload map[string]any
			_ = json.NewDecoder(request.Body).Decode(&payload)
			probeModel, _ = payload["model"].(string)
			_ = json.NewEncoder(writer).Encode(map[string]any{"data": []any{map[string]any{"b64_json": encoded}}})
		default:
			http.NotFound(writer, request)
		}
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
			BearerToken:    "sk-default",
			CredentialMode: "podotion_image_sk",
		}, nil
	}
	defer func() {
		providerTransport = originalTransport
		loadConfig = originalLoader
	}()
	output := t.TempDir()
	result, err := RunDoctor(context.Background(), DoctorOptions{ImageProbe: true, Model: "call-model", OutputDir: output})
	if err != nil {
		t.Fatal(err)
	}
	if result["ok"] != true || result["model"] != "call-model" {
		t.Fatalf("unexpected doctor result: %v", result)
	}
	capability := result["image_capability"].(map[string]any)
	if capability["available"] != true || capability["attempted"] != true {
		t.Fatalf("probe did not succeed: %v", capability)
	}
	if probeModel != "call-model" {
		t.Fatalf("probe used the wrong model: %s", probeModel)
	}
	images := capability["images"].([]map[string]any)
	if len(images) != 1 {
		t.Fatalf("probe did not save an image")
	}
}

func TestRunDoctorProbeFailureIsReported(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path == "/v1/models" {
			_, _ = writer.Write([]byte(`{}`))
			return
		}
		writer.WriteHeader(http.StatusUnauthorized)
		_, _ = writer.Write([]byte(`{"error":{"message":"bad sk-default"}}`))
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
			Model:          DefaultModel,
			BearerToken:    "sk-default",
			CredentialMode: "podotion_image_sk",
		}, nil
	}
	defer func() {
		providerTransport = originalTransport
		loadConfig = originalLoader
	}()
	result, err := RunDoctor(context.Background(), DoctorOptions{ImageProbe: true, OutputDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	if result["ok"] != false {
		t.Fatalf("probe failure should make doctor fail")
	}
	capability := result["image_capability"].(map[string]any)
	if capability["available"] != false {
		t.Fatalf("probe should not be available")
	}
	errorValue := capability["error"].(map[string]any)
	message, _ := errorValue["message"].(string)
	if strings.Contains(message, "sk-default") {
		t.Fatalf("probe error was not redacted: %s", message)
	}
}
