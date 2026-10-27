package podotion

import (
	"fmt"
	"path/filepath"
)

const (
	SiteName              = "Podotion"
	BinaryVersion         = "2.3.1"
	DefaultModel          = "gpt-image-2.5"
	LegacyDefaultModel    = "gpt-image-2"
	DirectProviderID      = "podotion-direct"
	DirectBaseURL         = "https://ai.podotion.com/v1"
	DirectConfigDirectory = "podotion-image"
	DirectConfigFilename  = "provider.toml"
	DefaultSecretKey      = "PodotionImageSk"
	FourKSecretKey        = "PodotionImage4kSk"
	DefaultSecretMark     = "__PODOTION_IMAGE_SK__"
	FourKSecretMark       = "__PODOTION_IMAGE_4K_SK__"
	ImagesGenerationsPath = "images/generations"
	ImagesEditsPath       = "images/edits"
	ModelsPath            = "models"
	Quality               = "auto"
	AutoSize              = "auto"
	DefaultTier           = "1k"
	TimeoutSeconds        = 600
	ProbeTimeoutSeconds   = 15
	RequestReuseSeconds   = 10 * 60
	RequestRecordVersion  = 1
	MaxJSONResponseBytes  = 75 * 1024 * 1024
	MaxErrorResponseBytes = 1024 * 1024
	MaxImageDownloadBytes = 50 * 1024 * 1024
	MaxInputFileBytes     = 20 * 1024 * 1024
	MaxPromptBytes        = 1024 * 1024
	MaxSecretBytes        = 64 * 1024
	MaxInputImages        = 5
	MaxEdge               = 3840
	MinPixels             = 655360
	MaxPixels             = 8294400
	DimensionMultiple     = 16
)

var SupportedTiers = map[string]int{
	"1k": 1024,
	"2k": 2048,
	"4k": 3840,
}

var SupportedRatios = map[string][2]int{
	"1:1":   {1, 1},
	"2:3":   {2, 3},
	"3:2":   {3, 2},
	"3:4":   {3, 4},
	"4:3":   {4, 3},
	"16:9":  {16, 9},
	"9:16":  {9, 16},
	"19:10": {19, 10},
	"10:19": {10, 19},
}

var fourKDimensionsByRatio = map[string][2]int{
	"1:1":   {2880, 2880},
	"2:3":   {2336, 3504},
	"3:2":   {3504, 2336},
	"3:4":   {2448, 3264},
	"4:3":   {3264, 2448},
	"16:9":  {3840, 2160},
	"9:16":  {2160, 3840},
	"19:10": {3648, 1920},
	"10:19": {1920, 3648},
}

var canonicalFourKDimensions = func() map[[2]int]bool {
	out := make(map[[2]int]bool, len(fourKDimensionsByRatio))
	for _, dims := range fourKDimensionsByRatio {
		out[dims] = true
	}
	return out
}()

type ResolvedSize struct {
	Value          string `json:"value"`
	RequestedSize  string `json:"requested_size,omitempty"`
	RequestedTier  string `json:"requested_tier,omitempty"`
	RequestedRatio string `json:"requested_ratio,omitempty"`
	Width          int    `json:"width,omitempty"`
	Height         int    `json:"height,omitempty"`
}

func (s ResolvedSize) Pixels() int {
	if s.Width <= 0 || s.Height <= 0 {
		return 0
	}
	return s.Width * s.Height
}

func (s ResolvedSize) CredentialProfile() string {
	if s.RequestedTier == "4k" {
		return "4k"
	}
	if s.Width > 0 && s.Height > 0 && canonicalFourKDimensions[[2]int{s.Width, s.Height}] {
		return "4k"
	}
	if s.Pixels() >= MaxPixels {
		return "4k"
	}
	return "default"
}

func (s ResolvedSize) AsMap() map[string]any {
	out := map[string]any{
		"value":           s.Value,
		"requested_size":  nil,
		"requested_tier":  nil,
		"requested_ratio": nil,
		"width":           nil,
		"height":          nil,
	}
	if s.RequestedSize != "" {
		out["requested_size"] = s.RequestedSize
	}
	if s.RequestedTier != "" {
		out["requested_tier"] = s.RequestedTier
	}
	if s.RequestedRatio != "" {
		out["requested_ratio"] = s.RequestedRatio
	}
	if s.Width > 0 {
		out["width"] = s.Width
		out["height"] = s.Height
	}
	return out
}

type ProviderConfig struct {
	ProviderID        string
	Name              string
	BaseURL           string
	Model             string
	BearerToken       string
	BearerToken4K     string
	CredentialMode    string
	ConfigPath        string
	CredentialProfile string
}

func (c ProviderConfig) SecretValues() []string {
	out := make([]string, 0, 2)
	if c.BearerToken != "" {
		out = append(out, c.BearerToken)
	}
	if c.BearerToken4K != "" {
		out = append(out, c.BearerToken4K)
	}
	return out
}

func (c ProviderConfig) SelectCredential(profile string) (ProviderConfig, error) {
	selected := c
	selected.CredentialProfile = profile
	switch profile {
	case "default":
		selected.BearerToken = c.BearerToken
		selected.CredentialMode = "podotion_image_sk"
	case "4k":
		if c.BearerToken4K == "" {
			return ProviderConfig{}, fmt.Errorf("4K image requests require %s; configure the 4K credential before retrying", FourKSecretKey)
		}
		selected.BearerToken = c.BearerToken4K
		selected.CredentialMode = "podotion_image_4k_sk"
	default:
		return ProviderConfig{}, fmt.Errorf("unsupported credential profile: %q", profile)
	}
	return selected, nil
}

type SavedImage struct {
	Path         string
	MarkdownPath string
	MimeType     string
	Bytes        int64
	Width        *int
	Height       *int
	SHA256       string `json:"sha256,omitempty"`
}

func NewSavedImage(path, mime string, bytes int64, width, height *int) SavedImage {
	abs, err := filepath.Abs(path)
	if err != nil {
		abs = path
	}
	return SavedImage{
		Path:         abs,
		MarkdownPath: filepath.ToSlash(abs),
		MimeType:     mime,
		Bytes:        bytes,
		Width:        width,
		Height:       height,
	}
}

func (s SavedImage) AsMap() map[string]any {
	var width any
	var height any
	if s.Width != nil {
		width = *s.Width
	}
	if s.Height != nil {
		height = *s.Height
	}
	out := map[string]any{
		"path":          s.Path,
		"markdown_path": s.MarkdownPath,
		"mime_type":     s.MimeType,
		"bytes":         s.Bytes,
		"width":         width,
		"height":        height,
	}
	if s.SHA256 != "" {
		out["sha256"] = s.SHA256
	}
	return out
}

type ImageResult struct {
	Value        string
	Kind         string
	Source       string
	MimeType     string
	OutputFormat string
}

type ProviderError struct {
	Kind              string
	Message           string
	HTTPStatus        *int
	RequestID         string
	CFRay             string
	ElapsedMS         int64
	Attempts          int
	RetryAfter        *float64
	FirstHTTPStatus   *int
	FirstErrorMessage string
	Details           map[string]any
}

func (e *ProviderError) Error() string {
	if e == nil {
		return ""
	}
	return e.Message
}

func (e *ProviderError) AsMap() map[string]any {
	out := map[string]any{
		"type":                "ProviderRequestError",
		"error_kind":          e.Kind,
		"message":             e.Message,
		"http_status":         nil,
		"request_id":          nil,
		"cf_ray":              nil,
		"elapsed_ms":          nil,
		"attempts":            e.Attempts,
		"retry_after":         nil,
		"first_http_status":   nil,
		"first_error_message": nil,
		"details":             e.Details,
	}
	if e.HTTPStatus != nil {
		out["http_status"] = *e.HTTPStatus
	}
	if e.RequestID != "" {
		out["request_id"] = e.RequestID
	}
	if e.CFRay != "" {
		out["cf_ray"] = e.CFRay
	}
	if e.ElapsedMS > 0 {
		out["elapsed_ms"] = e.ElapsedMS
	}
	if e.RetryAfter != nil {
		out["retry_after"] = *e.RetryAfter
	}
	if e.FirstHTTPStatus != nil {
		out["first_http_status"] = *e.FirstHTTPStatus
	}
	if e.FirstErrorMessage != "" {
		out["first_error_message"] = e.FirstErrorMessage
	}
	if out["details"] == nil {
		out["details"] = map[string]any{}
	}
	return out
}
