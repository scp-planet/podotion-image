package podotion

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

var base64Pattern = regexp.MustCompile(`^[A-Za-z0-9+/]*={0,2}$`)

var validateDownloadURL = ValidateDownloadURL

func ExtractImageResults(payload map[string]any) ([]ImageResult, error) {
	topKeys, unknownCount := safeResponseKeys(payload)
	makeError := func(message, selected string, count int) error {
		return &ProviderError{
			Kind:     "output_decode_error",
			Message:  message,
			Attempts: 1,
			Details: map[string]any{
				"top_level_keys":              topKeys,
				"unknown_top_level_key_count": unknownCount,
				"selected_shape":              selected,
				"candidate_count":             count,
			},
		}
	}
	if data, exists := payload["data"]; exists && data != nil {
		items, ok := data.([]any)
		if !ok {
			return nil, makeError("provider image response data must be an array", "$.data", 0)
		}
		if len(items) > 0 {
			if len(items) != 1 {
				return nil, makeError(fmt.Sprintf("provider image response must contain exactly one image; got %d", len(items)), "$.data", len(items))
			}
			item, ok := items[0].(map[string]any)
			if !ok {
				return nil, makeError("provider image response did not contain one supported final image", "$.data", 0)
			}
			result := resultFromItem(item, "$.data[0]", []string{"b64_json", "url"})
			if result == nil {
				return nil, makeError("provider image response did not contain one supported final image", "$.data", 0)
			}
			return []ImageResult{*result}, nil
		}
	}
	containers := []struct {
		source string
		items  any
	}{{"$.output", payload["output"]}}
	if response, ok := payload["response"].(map[string]any); ok {
		containers = append(containers, struct {
			source string
			items  any
		}{"$.response.output", response["output"]})
	}
	for _, container := range containers {
		items, ok := container.items.([]any)
		if !ok || len(items) == 0 {
			continue
		}
		results := responseImageCandidates(items, container.source)
		if len(results) != 1 {
			return nil, makeError(fmt.Sprintf("provider image response must contain exactly one final image; got %d", len(results)), container.source, len(results))
		}
		return results, nil
	}
	return nil, makeError("provider image response did not contain one supported final image", "", 0)
}

func safeResponseKeys(payload map[string]any) ([]string, int) {
	allowed := map[string]bool{
		"created": true, "data": true, "id": true, "model": true, "object": true,
		"output": true, "response": true, "status": true, "usage": true,
	}
	keys := make([]string, 0)
	for key := range payload {
		if allowed[key] {
			keys = append(keys, key)
		}
	}
	return keys, len(payload) - len(keys)
}

func responseImageCandidates(items []any, source string) []ImageResult {
	results := make([]ImageResult, 0)
	for index, raw := range items {
		item, ok := raw.(map[string]any)
		if !ok || item["type"] != "image_generation_call" {
			continue
		}
		if statusValue, exists := item["status"]; exists {
			status, ok := statusValue.(string)
			if !ok || strings.ToLower(strings.TrimSpace(status)) != "completed" {
				continue
			}
		}
		if _, exists := item["partial_image"]; exists {
			continue
		}
		if _, exists := item["partial_image_b64"]; exists {
			continue
		}
		result := resultFromItem(item, fmt.Sprintf("%s[%d]", source, index), []string{"result", "b64_json", "url"})
		if result != nil {
			results = append(results, *result)
		}
	}
	return results
}

func resultFromItem(item map[string]any, source string, fields []string) *ImageResult {
	mimeType, _ := item["mime_type"].(string)
	outputFormat, _ := item["output_format"].(string)
	for _, field := range fields {
		value, ok := item[field].(string)
		if !ok || strings.TrimSpace(value) == "" {
			continue
		}
		normalized := strings.TrimSpace(value)
		resultField := field
		if field == "url" || strings.HasPrefix(strings.ToLower(normalized), "https://") {
			resultField = "url"
		}
		return &ImageResult{
			Value:        normalized,
			Kind:         classifyResultValue(normalized, resultField),
			Source:       source + "." + field,
			MimeType:     strings.TrimSpace(mimeType),
			OutputFormat: strings.TrimSpace(outputFormat),
		}
	}
	return nil
}

func classifyResultValue(value, field string) string {
	if field == "url" {
		return "url"
	}
	if strings.HasPrefix(value, "data:image/") {
		return "data_url"
	}
	return "base64"
}

func ValidateDownloadURL(value string) error {
	parsed, err := url.Parse(value)
	if err != nil {
		return fmt.Errorf("image result URL is invalid")
	}
	if parsed.Scheme != "https" || parsed.Host == "" {
		return fmt.Errorf("image result URL must be an absolute HTTPS URL")
	}
	if parsed.User != nil {
		return fmt.Errorf("image result URL must not contain embedded credentials")
	}
	hostname := strings.ToLower(parsed.Hostname())
	if hostname == "" || hostname == "localhost" || strings.HasSuffix(hostname, ".local") {
		return fmt.Errorf("image result URL points to a local address")
	}
	if address := net.ParseIP(hostname); address != nil {
		if !isPublicIP(address) {
			return fmt.Errorf("image result URL points to a non-public address")
		}
		return nil
	}
	addresses, err := net.LookupIP(hostname)
	if err != nil {
		return fmt.Errorf("image result URL hostname could not be resolved")
	}
	if len(addresses) == 0 {
		return fmt.Errorf("image result URL hostname could not be resolved")
	}
	for _, address := range addresses {
		if !isPublicIP(address) {
			return fmt.Errorf("image result URL resolves to a non-public address")
		}
	}
	return nil
}

func isPublicIP(address net.IP) bool {
	if address == nil || address.IsUnspecified() || address.IsLoopback() || address.IsPrivate() || address.IsLinkLocalUnicast() || address.IsMulticast() {
		return false
	}
	return address.IsGlobalUnicast()
}
func DecodeImageResult(ctx context.Context, result ImageResult, timeout time.Duration) ([]byte, string, string, error) {
	var data []byte
	var contentType string
	switch result.Kind {
	case "url":
		if err := validateDownloadURL(result.Value); err != nil {
			return nil, "", "", err
		}
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, result.Value, nil)
		if err != nil {
			return nil, "", "", err
		}
		request.Header.Set("Accept", "image/avif,image/webp,image/png,image/jpeg,*/*")
		request.Header.Set("User-Agent", "podotion-image-skill/"+BinaryVersion)
		client := newHTTPClient(timeout, func(request *http.Request, _ []*http.Request) error {
			return validateDownloadURL(request.URL.String())
		})
		response, err := client.Do(request)
		if err != nil {
			return nil, "", "", fmt.Errorf("image result URL could not be downloaded: %w", err)
		}
		defer response.Body.Close()
		contentType = response.Header.Get("Content-Type")
		if !strings.HasPrefix(strings.ToLower(contentType), "image/") {
			return nil, "", "", fmt.Errorf("image download returned unexpected content type %q", contentType)
		}
		data, err = readLimited(response.Body, MaxImageDownloadBytes)
		if err != nil {
			return nil, "", "", err
		}
	case "data_url":
		header, encoded, ok := strings.Cut(result.Value, ",")
		if !ok || !strings.HasPrefix(strings.ToLower(header), "data:image/") || !strings.Contains(strings.ToLower(header), ";base64") {
			return nil, "", "", fmt.Errorf("image result contained an invalid data URL")
		}
		var err error
		data, err = decodeBase64Image(encoded)
		if err != nil {
			return nil, "", "", err
		}
		contentType = header[5:]
		contentType, _, _ = strings.Cut(contentType, ";")
	case "base64":
		var err error
		data, err = decodeBase64Image(result.Value)
		if err != nil {
			return nil, "", "", err
		}
	default:
		return nil, "", "", fmt.Errorf("unsupported image result kind")
	}
	if len(data) == 0 {
		return nil, "", "", fmt.Errorf("image result was empty")
	}
	if len(data) > MaxImageDownloadBytes {
		return nil, "", "", fmt.Errorf("decoded image exceeded the 50 MB safety limit")
	}
	mimeType, suffix, err := DetectImageType(data)
	if err != nil {
		return nil, "", "", err
	}
	if contentType != "" && !strings.HasPrefix(strings.ToLower(contentType), "image/") {
		return nil, "", "", fmt.Errorf("image result had an unsupported content type")
	}
	return data, mimeType, suffix, nil
}

func decodeBase64Image(value string) ([]byte, error) {
	encoded := strings.Join(strings.Fields(value), "")
	if len(encoded) < 32 {
		return nil, fmt.Errorf("image result base64 data was too short")
	}
	maxEncoded := 4 * ((MaxImageDownloadBytes + 2) / 3)
	if len(encoded) > maxEncoded {
		return nil, fmt.Errorf("image result base64 data exceeded the encoded size limit")
	}
	if len(encoded)%4 != 0 || !base64Pattern.MatchString(encoded) {
		return nil, fmt.Errorf("image result contained invalid base64 characters or padding")
	}
	data, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return nil, fmt.Errorf("image result contained invalid base64 data")
	}
	return data, nil
}

func SaveImageResults(ctx context.Context, results []ImageResult, outputDir string, timeout time.Duration) ([]SavedImage, []map[string]any, error) {
	if len(results) != 1 {
		return nil, nil, fmt.Errorf("exactly one image result is required; got %d", len(results))
	}
	data, mimeType, suffix, err := DecodeImageResult(ctx, results[0], timeout)
	if err != nil {
		return nil, nil, &ProviderError{
			Kind:     "output_decode_error",
			Message:  "the image result was invalid",
			Attempts: 1,
			Details: map[string]any{
				"code":            "invalid_image_candidate",
				"result_index":    1,
				"candidate_count": 1,
				"source":          results[0].Source,
				"value_length":    len(results[0].Value),
				"reason":          err.Error(),
			},
		}
	}
	absoluteDir, err := filepath.Abs(outputDir)
	if err != nil {
		return nil, nil, err
	}
	if err := os.MkdirAll(absoluteDir, 0o755); err != nil {
		return nil, nil, fmt.Errorf("failed to create the output directory: %w", err)
	}
	stamp := time.Now().Format("20060102_150405")
	var filename, destination string
	for attempt := 0; ; attempt++ {
		filename = stamp + "-" + RandomSuffix() + suffix
		destination = filepath.Join(absoluteDir, filename)
		reservation, reserveErr := os.OpenFile(destination, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
		if reserveErr == nil {
			reservation.Close()
			break
		}
		if !os.IsExist(reserveErr) || attempt >= 100 {
			return nil, nil, fmt.Errorf("cannot reserve generated image destination: %w", reserveErr)
		}
	}
	committed := false
	defer func() {
		if !committed {
			_ = os.Remove(destination)
		}
	}()
	temp, err := os.CreateTemp(absoluteDir, "."+filename+".*.tmp")
	if err != nil {
		return nil, nil, fmt.Errorf("failed to create a temporary image: %w", err)
	}
	tempPath := temp.Name()
	defer os.Remove(tempPath)
	_ = temp.Chmod(0o600)
	if _, err := temp.Write(data); err != nil {
		temp.Close()
		return nil, nil, fmt.Errorf("failed to write the decoded image: %w", err)
	}
	if err := temp.Sync(); err != nil {
		temp.Close()
		return nil, nil, fmt.Errorf("failed to sync the decoded image: %w", err)
	}
	if err := temp.Close(); err != nil {
		return nil, nil, fmt.Errorf("failed to close the decoded image: %w", err)
	}
	if err := os.Rename(tempPath, destination); err != nil {
		return nil, nil, fmt.Errorf("failed to atomically save the decoded image: %w", err)
	}
	committed = true
	width, height := PNGDimensions(data)
	image := NewSavedImage(destination, mimeType, int64(len(data)), width, height)
	return []SavedImage{image}, nil, nil
}

func PNGDimensions(data []byte) (*int, *int) {
	if len(data) < 24 || !bytes.HasPrefix(data, []byte{0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a}) {
		return nil, nil
	}
	width := int(binary.BigEndian.Uint32(data[16:20]))
	height := int(binary.BigEndian.Uint32(data[20:24]))
	return &width, &height
}

func ImageSizeWarnings(requestedSize string, images []SavedImage) []map[string]any {
	if requestedSize == AutoSize {
		return nil
	}
	expectedWidth, expectedHeight, err := ParseExactSize(requestedSize)
	if err != nil {
		return nil
	}
	warnings := make([]map[string]any, 0)
	for index, image := range images {
		if image.Width == nil || image.Height == nil {
			continue
		}
		if *image.Width == expectedWidth && *image.Height == expectedHeight {
			continue
		}
		warnings = append(warnings, map[string]any{
			"code":           "image_size_mismatch",
			"message":        "provider returned different pixel dimensions than requested",
			"image_index":    index + 1,
			"requested_size": requestedSize,
			"actual_size":    FormatSize(*image.Width, *image.Height),
		})
	}
	return warnings
}

func SHA256File(path string) (string, error) {
	handle, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer handle.Close()
	digest := sha256.New()
	if _, err := io.Copy(digest, handle); err != nil {
		return "", err
	}
	return hex.EncodeToString(digest.Sum(nil)), nil
}
