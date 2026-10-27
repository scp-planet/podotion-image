package podotion

import (
	"regexp"
	"strings"
)

var bearerPattern = regexp.MustCompile(`(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;"']+`)

func RedactSecrets(text string, secrets ...string) string {
	redacted := text
	for _, secret := range secrets {
		if secret != "" {
			redacted = strings.ReplaceAll(redacted, secret, "<redacted>")
		}
	}
	return bearerPattern.ReplaceAllString(redacted, `${1}<redacted>`)
}

func SanitizeValue(value any, secrets ...string) any {
	switch v := value.(type) {
	case string:
		return RedactSecrets(v, secrets...)
	case map[string]any:
		out := make(map[string]any, len(v))
		for key, child := range v {
			out[key] = SanitizeValue(child, secrets...)
		}
		return out
	case []any:
		out := make([]any, len(v))
		for i, child := range v {
			out[i] = SanitizeValue(child, secrets...)
		}
		return out
	default:
		return value
	}
}
