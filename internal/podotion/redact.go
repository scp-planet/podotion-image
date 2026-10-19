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
