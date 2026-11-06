package podotion

import (
	"fmt"
	"strings"
)

const DefaultOutputFormat = "png"

func ResolveOutputFormat(operation, value string) (string, error) {
	format := strings.ToLower(strings.TrimSpace(value))
	if format == "" {
		format = DefaultOutputFormat
	}
	if format == "jpg" {
		format = "jpeg"
	}
	switch format {
	case "png", "jpeg", "webp", "gif":
	default:
		return "", fmt.Errorf("output format must be png, jpg, jpeg, webp, or gif")
	}
	if operation != "generate" && operation != "edit" {
		return "", fmt.Errorf("operation must be generate or edit")
	}
	return format, nil
}

func imageOutputFormat(mimeType string) string {
	return strings.TrimPrefix(mimeType, "image/")
}

func ImageFormatWarnings(requested string, images []SavedImage) []map[string]any {
	warnings := []map[string]any{}
	for index, image := range images {
		actual := imageOutputFormat(image.MimeType)
		if actual != requested {
			warnings = append(warnings, map[string]any{
				"code":             "image_format_mismatch",
				"message":          "provider returned a different format; original bytes and actual extension were preserved",
				"image_index":      index + 1,
				"requested_format": requested,
				"actual_format":    actual,
			})
		}
	}
	return warnings
}
