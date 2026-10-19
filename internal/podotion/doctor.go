package podotion

import (
	"context"
	"path/filepath"
	"time"
)

type DoctorOptions struct {
	CredentialFile string
	Model          string
	ImageProbe     bool
	OutputDir      string
}

func RunDoctor(ctx context.Context, opts DoctorOptions) (map[string]any, error) {
	config, err := loadConfig(opts.CredentialFile)
	if err != nil {
		return nil, err
	}
	model := ResolveModel(opts.Model, ModelSource{Model: config.Model})
	model, err = ValidateModel(model)
	if err != nil {
		return nil, err
	}
	config.Model = model
	defaultProvider, err := config.SelectCredential("default")
	if err != nil {
		return nil, err
	}
	configured := map[string]ProviderConfig{"default": defaultProvider}
	if config.BearerToken4K != "" {
		fourKProvider, selectErr := config.SelectCredential("4k")
		if selectErr == nil {
			configured["4k"] = fourKProvider
		}
	}
	profileReports := map[string]any{}
	for _, profile := range []string{"default", "4k"} {
		provider, ok := configured[profile]
		if !ok {
			continue
		}
		connectivity := ProbeProvider(ctx, provider, ProbeTimeoutSeconds*time.Second)
		profileReports[profile] = map[string]any{
			"configured":      true,
			"credential_mode": provider.CredentialMode,
			"reachable":       boolValue(connectivity["reachable"]),
			"http_status":     connectivity["http_status"],
			"connectivity":    connectivity,
		}
	}
	warnings := []map[string]any{}
	if _, ok := configured["4k"]; !ok {
		profileReports["4k"] = map[string]any{
			"configured":      false,
			"credential_mode": "podotion_image_4k_sk",
			"reachable":       nil,
			"http_status":     nil,
			"connectivity":    nil,
		}
		warnings = append(warnings, map[string]any{
			"code":    "optional_4k_credential_missing",
			"message": FourKSecretKey + " is not configured; 4K requests are unavailable",
		})
	}

	capability := map[string]any{
		"attempted": false,
		"may_bill":  false,
		"note":      "use --image-probe to test billable image generation",
	}
	if opts.ImageProbe {
		capability = runImageProbe(ctx, defaultProvider, model, opts.OutputDir)
	}

	profilesReachable := true
	for _, report := range profileReports {
		entry, ok := report.(map[string]any)
		if ok && boolValue(entry["configured"]) && !boolValue(entry["reachable"]) {
			profilesReachable = false
		}
	}
	ok := profilesReachable && (!opts.ImageProbe || boolValue(capability["available"]))
	generateEndpoint, _ := BuildImagesURL(defaultProvider.BaseURL, "generate")
	editEndpoint, _ := BuildImagesURL(defaultProvider.BaseURL, "edit")
	outputDir, _ := ResolveOutputDir("")
	return map[string]any{
		"ok":              ok,
		"operation":       "doctor",
		"site":            SiteName,
		"provider_id":     defaultProvider.ProviderID,
		"provider_name":   defaultProvider.Name,
		"base_url":        defaultProvider.BaseURL,
		"credential_mode": defaultProvider.CredentialMode,
		"config_path":     defaultProvider.ConfigPath,
		"transport":       "images",
		"endpoints": map[string]any{
			"generate": generateEndpoint,
			"edit":     editEndpoint,
		},
		"model":               model,
		"output_directory":    outputDir,
		"connectivity":        profileReports["default"].(map[string]any)["connectivity"],
		"credential_profiles": profileReports,
		"image_capability":    capability,
		"warnings":            warnings,
	}, nil
}

func runImageProbe(ctx context.Context, provider ProviderConfig, model, outputDir string) map[string]any {
	capability := map[string]any{
		"attempted":    true,
		"may_bill":     true,
		"max_attempts": 1,
		"available":    false,
	}
	size, err := ResolveSize("1k", "1:1")
	if err != nil {
		capability["error"] = map[string]any{"type": "local_error", "message": err.Error()}
		return capability
	}
	provider.Model = model
	response, err := PostImages(ctx, provider, "generate", "Generate a plain light gray square with no text.", nil, size.Value)
	if err != nil {
		capability["error"] = asProviderError(err).AsMap()
		return capability
	}
	results, err := ExtractImageResults(response)
	if err != nil {
		capability["error"] = asProviderError(err).AsMap()
		return capability
	}
	if outputDir == "" {
		cwdOutput, outputErr := ResolveOutputDir("")
		if outputErr != nil {
			capability["error"] = map[string]any{"type": "local_error", "message": outputErr.Error()}
			return capability
		}
		outputDir = filepath.Join(cwdOutput, "doctor-probe")
	} else {
		absolute, outputErr := filepath.Abs(outputDir)
		if outputErr != nil {
			capability["error"] = map[string]any{"type": "local_error", "message": outputErr.Error()}
			return capability
		}
		outputDir = absolute
	}
	saved, warnings, err := SaveImageResults(ctx, results, outputDir, TimeoutSeconds*time.Second)
	if err != nil {
		capability["error"] = asProviderError(err).AsMap()
		return capability
	}
	warnings = append(warnings, ImageSizeWarnings(size.Value, saved)...)
	images := make([]map[string]any, 0, len(saved))
	for _, image := range saved {
		images = append(images, image.AsMap())
	}
	endpoint, _ := BuildImagesURL(provider.BaseURL, "generate")
	capability["available"] = true
	capability["endpoint"] = endpoint
	capability["image_result_count"] = len(saved)
	capability["images"] = images
	capability["warnings"] = warnings
	return capability
}

func boolValue(value any) bool {
	parsed, _ := value.(bool)
	return parsed
}
