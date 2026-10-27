package podotion

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/textproto"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

var providerTransport http.RoundTripper

func newHTTPClient(timeout time.Duration, checkRedirect func(*http.Request, []*http.Request) error) *http.Client {
	transport := providerTransport
	if transport == nil {
		transport = http.DefaultTransport
	}
	return &http.Client{Timeout: timeout, Transport: transport, CheckRedirect: checkRedirect}
}

func BuildImagesURL(baseURL, operation string) (string, error) {
	var endpoint string
	switch operation {
	case "generate":
		endpoint = ImagesGenerationsPath
	case "edit":
		endpoint = ImagesEditsPath
	default:
		return "", fmt.Errorf("operation must be generate or edit")
	}
	base := strings.TrimRight(strings.TrimSpace(baseURL), "/")
	parsed, err := url.Parse(base)
	if err != nil {
		return "", fmt.Errorf("invalid provider base URL")
	}
	if parsed.Scheme != "https" || parsed.Host == "" {
		return "", fmt.Errorf("provider base URL must be an absolute HTTPS URL")
	}
	if parsed.User != nil {
		return "", fmt.Errorf("provider base URL must not contain credentials")
	}
	path := strings.TrimRight(parsed.Path, "/")
	if strings.HasSuffix(path, "/images/generations") || strings.HasSuffix(path, "/images/edits") {
		path = path[:strings.LastIndex(path, "/images/")]
	}
	if !strings.HasSuffix(path, "/v1") {
		path += "/v1"
	}
	parsed.Path = path + "/" + endpoint
	parsed.RawQuery = ""
	parsed.Fragment = ""
	return parsed.String(), nil
}

func BuildModelsURL(baseURL string) (string, error) {
	base := strings.TrimRight(strings.TrimSpace(baseURL), "/")
	parsed, err := url.Parse(base)
	if err != nil || parsed.Scheme != "https" || parsed.Host == "" {
		return "", fmt.Errorf("invalid provider base URL")
	}
	path := strings.TrimRight(parsed.Path, "/")
	if !strings.HasSuffix(path, "/v1") {
		path += "/v1"
	}
	parsed.Path = path + "/" + ModelsPath
	parsed.RawQuery = ""
	parsed.Fragment = ""
	return parsed.String(), nil
}

func BuildImagesGenerationPayload(model, prompt, size string) ([]byte, error) {
	normalizedPrompt, err := normalizePrompt(prompt)
	if err != nil {
		return nil, err
	}
	payload := map[string]any{
		"model":         model,
		"prompt":        normalizedPrompt,
		"size":          size,
		"quality":       Quality,
		"output_format": "png",
		"n":             1,
	}
	return json.Marshal(payload)
}

func BuildImagesEditMultipart(model, prompt string, imagePaths []string, size string) ([]byte, string, error) {
	images, err := prepareInputImages(imagePaths)
	if err != nil {
		return nil, "", err
	}
	return buildPreparedEditMultipart(model, prompt, images, size)
}

func buildPreparedEditMultipart(model, prompt string, images []uploadImage, size string) ([]byte, string, error) {
	normalizedPrompt, err := normalizePrompt(prompt)
	if err != nil {
		return nil, "", err
	}
	if len(images) == 0 {
		return nil, "", fmt.Errorf("edit operation requires at least one input image")
	}
	if len(images) > MaxInputImages {
		return nil, "", fmt.Errorf("at most %d input images are supported", MaxInputImages)
	}
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	fields := []struct {
		name  string
		value string
	}{
		{"model", model},
		{"prompt", normalizedPrompt},
		{"size", size},
		{"quality", Quality},
		{"output_format", "png"},
		{"n", "1"},
	}
	for _, field := range fields {
		if err := writer.WriteField(field.name, field.value); err != nil {
			return nil, "", err
		}
	}
	for index, input := range images {
		header := make(textproto.MIMEHeader)
		header.Set("Content-Disposition", fmt.Sprintf(`form-data; name="image[]"; filename="image-%d%s"`, index+1, input.Suffix))
		header.Set("Content-Type", input.MIME)
		part, err := writer.CreatePart(header)
		if err != nil {
			return nil, "", err
		}
		if _, err := part.Write(input.Data); err != nil {
			return nil, "", err
		}
	}
	if err := writer.Close(); err != nil {
		return nil, "", err
	}
	return body.Bytes(), writer.FormDataContentType(), nil
}

func normalizePrompt(prompt string) (string, error) {
	normalized := strings.TrimSpace(prompt)
	if !utf8.ValidString(normalized) {
		return "", fmt.Errorf("prompt must be valid UTF-8")
	}
	if normalized == "" {
		return "", fmt.Errorf("prompt cannot be empty")
	}
	if len([]byte(normalized)) > MaxPromptBytes {
		return "", fmt.Errorf("prompt exceeds the 1 MB safety limit")
	}
	return normalized, nil
}

func ValidateInputFile(path string) error {
	info, err := os.Stat(path)
	if err != nil {
		if os.IsNotExist(err) {
			return fmt.Errorf("image file not found: %s", path)
		}
		return err
	}
	if info.IsDir() {
		return fmt.Errorf("image path is a directory: %s", path)
	}
	if info.Size() <= 0 {
		return fmt.Errorf("image file is empty: %s", path)
	}
	if info.Size() > MaxInputFileBytes {
		return fmt.Errorf("image file exceeds the 20 MB safety limit: %s", path)
	}
	return nil
}

func DetectImageType(data []byte) (string, string, error) {
	if bytes.HasPrefix(data, []byte{0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a}) {
		return "image/png", ".png", nil
	}
	if bytes.HasPrefix(data, []byte{0xff, 0xd8, 0xff}) {
		return "image/jpeg", ".jpg", nil
	}
	if bytes.HasPrefix(data, []byte("GIF87a")) || bytes.HasPrefix(data, []byte("GIF89a")) {
		return "image/gif", ".gif", nil
	}
	if len(data) >= 12 && string(data[:4]) == "RIFF" && string(data[8:12]) == "WEBP" {
		return "image/webp", ".webp", nil
	}
	return "", "", fmt.Errorf("image data had an unsupported or invalid file signature")
}

func PostImages(ctx context.Context, provider ProviderConfig, operation, prompt string, imagePaths []string, size string) (map[string]any, error) {
	var body []byte
	var contentType string
	var err error
	if operation == "generate" {
		if len(imagePaths) > 0 {
			return nil, fmt.Errorf("generate operation does not accept input images")
		}
		body, err = BuildImagesGenerationPayload(provider.Model, prompt, size)
		if err != nil {
			return nil, err
		}
		contentType = "application/json"
	} else if operation == "edit" {
		body, contentType, err = BuildImagesEditMultipart(provider.Model, prompt, imagePaths, size)
		if err != nil {
			return nil, err
		}
	} else {
		return nil, fmt.Errorf("operation must be generate or edit")
	}
	endpoint, err := BuildImagesURL(provider.BaseURL, operation)
	if err != nil {
		return nil, err
	}
	return postProviderJSON(ctx, provider, endpoint, body, contentType, prompt)
}

func postProviderJSON(ctx context.Context, provider ProviderConfig, endpoint string, body []byte, contentType, prompt string) (map[string]any, error) {
	started := time.Now()
	requestCtx, cancel := context.WithTimeout(ctx, TimeoutSeconds*time.Second)
	defer cancel()
	request, err := http.NewRequestWithContext(requestCtx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	request.Header.Set("Authorization", "Bearer "+provider.BearerToken)
	request.Header.Set("Content-Type", contentType)
	request.Header.Set("Accept", "application/json")
	request.Header.Set("User-Agent", "podotion-image-skill/"+BinaryVersion)
	client := newHTTPClient(TimeoutSeconds*time.Second, func(_ *http.Request, _ []*http.Request) error {
		return http.ErrUseLastResponse
	})
	response, err := client.Do(request)
	if err != nil {
		kind := "upstream_error"
		message := RedactSecrets("provider request failed: "+err.Error(), append(provider.SecretValues(), prompt)...)
		return nil, &ProviderError{Kind: kind, Message: message, Attempts: 1, ElapsedMS: time.Since(started).Milliseconds(), Details: map[string]any{}}
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		raw, readErr := readLimited(response.Body, MaxErrorResponseBytes)
		message := fmt.Sprintf("provider request failed with HTTP %d", response.StatusCode)
		if readErr == nil {
			if detail := upstreamErrorMessage(raw); detail != "" {
				message += ": " + detail
			}
		}
		message = RedactSecrets(message, append(provider.SecretValues(), prompt)...)
		status := response.StatusCode
		errorKind := "upstream_error"
		return nil, &ProviderError{
			Kind:              errorKind,
			Message:           message,
			HTTPStatus:        &status,
			RequestID:         RedactSecrets(firstHeader(response.Header, "x-request-id", "openai-request-id"), provider.SecretValues()...),
			CFRay:             RedactSecrets(firstHeader(response.Header, "cf-ray"), provider.SecretValues()...),
			ElapsedMS:         time.Since(started).Milliseconds(),
			Attempts:          1,
			FirstHTTPStatus:   &status,
			FirstErrorMessage: message,
			Details:           map[string]any{},
		}
	}
	raw, err := readLimited(response.Body, MaxJSONResponseBytes)
	if err != nil {
		return nil, &ProviderError{Kind: "output_decode_error", Message: err.Error(), Attempts: 1, ElapsedMS: time.Since(started).Milliseconds(), Details: map[string]any{}}
	}
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, &ProviderError{Kind: "output_decode_error", Message: "provider response was not valid UTF-8 JSON", Attempts: 1, ElapsedMS: time.Since(started).Milliseconds(), Details: map[string]any{}}
	}
	return payload, nil
}

func ProbeProvider(ctx context.Context, provider ProviderConfig, timeout time.Duration) map[string]any {
	endpoint, err := BuildModelsURL(provider.BaseURL)
	if err != nil {
		return map[string]any{"reachable": false, "http_status": nil, "endpoint": "", "error": err.Error()}
	}
	requestCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	request, err := http.NewRequestWithContext(requestCtx, http.MethodGet, endpoint, nil)
	if err != nil {
		return map[string]any{"reachable": false, "http_status": nil, "endpoint": endpoint, "error": "invalid request"}
	}
	request.Header.Set("Authorization", "Bearer "+provider.BearerToken)
	request.Header.Set("User-Agent", "podotion-image-skill/"+BinaryVersion)
	client := newHTTPClient(timeout, func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse })
	response, err := client.Do(request)
	if err != nil {
		return map[string]any{"reachable": false, "http_status": nil, "endpoint": endpoint, "error": RedactSecrets(err.Error(), provider.SecretValues()...)}
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 4096))
	return map[string]any{"reachable": true, "http_status": response.StatusCode, "endpoint": endpoint}
}

func readLimited(reader io.Reader, limit int64) ([]byte, error) {
	data, err := io.ReadAll(io.LimitReader(reader, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > limit {
		return nil, fmt.Errorf("provider response exceeded the local safety limit")
	}
	return data, nil
}

func upstreamErrorMessage(raw []byte) string {
	var payload any
	if err := json.Unmarshal(raw, &payload); err == nil {
		if message := findErrorMessage(payload); message != "" {
			if len(message) > 500 {
				message = message[:500]
			}
			return message
		}
	}
	text := strings.TrimSpace(string(raw))
	text = strings.ReplaceAll(text, "\r", " ")
	text = strings.ReplaceAll(text, "\n", " ")
	if len(text) > 500 {
		text = text[:500]
	}
	return text
}

func findErrorMessage(value any) string {
	switch typed := value.(type) {
	case map[string]any:
		if errorValue, ok := typed["error"]; ok {
			if message := findErrorMessage(errorValue); message != "" {
				return message
			}
		}
		if message, ok := typed["message"].(string); ok && strings.TrimSpace(message) != "" {
			return strings.TrimSpace(message)
		}
		for _, child := range typed {
			if message := findErrorMessage(child); message != "" {
				return message
			}
		}
	case []any:
		for _, child := range typed {
			if message := findErrorMessage(child); message != "" {
				return message
			}
		}
	case string:
		return strings.TrimSpace(typed)
	}
	return ""
}

func firstHeader(headers http.Header, names ...string) string {
	for _, name := range names {
		if value := strings.TrimSpace(headers.Get(name)); value != "" {
			return value
		}
	}
	return ""
}

func ResolveOutputDir(value string) (string, error) {
	if strings.TrimSpace(value) != "" {
		abs, err := filepath.Abs(strings.TrimSpace(value))
		if err != nil {
			return "", err
		}
		return abs, nil
	}
	cwd, err := os.Getwd()
	if err != nil {
		return "", err
	}
	return filepath.Join(cwd, "PodotionImageOutput"), nil
}

func FormatSize(width, height int) string {
	return strconv.Itoa(width) + "x" + strconv.Itoa(height)
}
