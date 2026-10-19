package podotion

import (
	"bufio"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

var modelPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$`)

func DefaultConfigPath() (string, error) {
	if configured := strings.TrimSpace(os.Getenv("CODEX_HOME")); configured != "" {
		return filepath.Join(configured, DirectConfigDirectory, DirectConfigFilename), nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("cannot locate the user home directory: %w", err)
	}
	return filepath.Join(home, ".codex", DirectConfigDirectory, DirectConfigFilename), nil
}

func ConfigPath(explicit string) (string, error) {
	if strings.TrimSpace(explicit) != "" {
		abs, err := filepath.Abs(strings.TrimSpace(explicit))
		if err != nil {
			return "", err
		}
		return abs, nil
	}
	path, err := DefaultConfigPath()
	if err != nil {
		return "", err
	}
	return filepath.Abs(path)
}

func LoadConfig(path string) (ProviderConfig, error) {
	target, err := ConfigPath(path)
	if err != nil {
		return ProviderConfig{}, err
	}
	info, err := os.Stat(target)
	if err != nil {
		if os.IsNotExist(err) {
			return ProviderConfig{}, fmt.Errorf("Podotion image credential file not found: %s; run the config command first", target)
		}
		return ProviderConfig{}, fmt.Errorf("cannot inspect the Podotion image credential file: %w", err)
	}
	if info.Size() > MaxErrorResponseBytes {
		return ProviderConfig{}, fmt.Errorf("Podotion image credential file exceeds the 1 MB safety limit")
	}
	raw, err := os.ReadFile(target)
	if err != nil {
		return ProviderConfig{}, fmt.Errorf("cannot read the Podotion image credential file: %w", err)
	}
	values, err := parseFlatTOML(string(raw))
	if err != nil {
		return ProviderConfig{}, fmt.Errorf("failed to parse Podotion image credential file: %w", err)
	}
	allowed := map[string]bool{
		"base_url":       true,
		"model":          true,
		DefaultSecretKey: true,
		FourKSecretKey:   true,
	}
	for key := range values {
		if !allowed[key] {
			return ProviderConfig{}, fmt.Errorf("Podotion image credential file contains unsupported key %q", key)
		}
	}
	baseURL := values["base_url"]
	if baseURL != DirectBaseURL {
		return ProviderConfig{}, fmt.Errorf("direct provider base_url must be exactly %s; the direct endpoint cannot be overridden", DirectBaseURL)
	}
	token, err := validateSecret(values[DefaultSecretKey], DefaultSecretKey)
	if err != nil {
		return ProviderConfig{}, err
	}
	token4k := ""
	if raw4k, ok := values[FourKSecretKey]; ok {
		token4k, err = validateSecret(raw4k, FourKSecretKey)
		if err != nil {
			return ProviderConfig{}, err
		}
	}
	model := strings.TrimSpace(values["model"])
	if model == "" {
		model = LegacyDefaultModel
	}
	model, err = ValidateModel(model)
	if err != nil {
		return ProviderConfig{}, err
	}
	return ProviderConfig{
		ProviderID:     DirectProviderID,
		Name:           SiteName,
		BaseURL:        DirectBaseURL,
		Model:          model,
		BearerToken:    token,
		BearerToken4K:  token4k,
		CredentialMode: "podotion_image_sk",
		ConfigPath:     target,
	}, nil
}

func parseFlatTOML(content string) (map[string]string, error) {
	values := map[string]string{}
	scanner := bufio.NewScanner(strings.NewReader(strings.TrimPrefix(content, "\ufeff")))
	scanner.Buffer(make([]byte, 1024), MaxErrorResponseBytes)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if strings.HasPrefix(line, "[") {
			return nil, fmt.Errorf("TOML tables are not supported in provider.toml")
		}
		index := strings.Index(line, "=")
		if index <= 0 {
			return nil, fmt.Errorf("invalid TOML assignment")
		}
		key := strings.TrimSpace(line[:index])
		value := strings.TrimSpace(stripTOMLComment(line[index+1:]))
		if key == "" || value == "" {
			return nil, fmt.Errorf("invalid TOML assignment")
		}
		if _, exists := values[key]; exists {
			return nil, fmt.Errorf("duplicate TOML key %q", key)
		}
		parsed, err := parseTOMLString(value)
		if err != nil {
			return nil, err
		}
		values[key] = parsed
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	return values, nil
}

func stripTOMLComment(value string) string {
	inDouble := false
	inSingle := false
	escaped := false
	for index, character := range value {
		if escaped {
			escaped = false
			continue
		}
		if character == '\\' && inDouble {
			escaped = true
			continue
		}
		if character == '"' && !inSingle {
			inDouble = !inDouble
			continue
		}
		if character == '\'' && !inDouble {
			inSingle = !inSingle
			continue
		}
		if character == '#' && !inDouble && !inSingle {
			return value[:index]
		}
	}
	return value
}
func parseTOMLString(value string) (string, error) {
	if strings.HasPrefix(value, `"`) {
		if !strings.HasSuffix(value, `"`) || len(value) < 2 {
			return "", fmt.Errorf("invalid TOML string")
		}
		parsed, err := strconv.Unquote(value)
		if err != nil {
			return "", fmt.Errorf("invalid TOML string")
		}
		return parsed, nil
	}
	if strings.HasPrefix(value, `'`) {
		if !strings.HasSuffix(value, `'`) || len(value) < 2 {
			return "", fmt.Errorf("invalid TOML literal string")
		}
		return value[1 : len(value)-1], nil
	}
	return "", fmt.Errorf("TOML values must be quoted strings")
}

func validateSecret(value, key string) (string, error) {
	secret := strings.TrimSpace(value)
	if secret == "" {
		return "", fmt.Errorf("Podotion image credential %s is missing or empty", key)
	}
	if strings.ContainsAny(secret, "\r\n") {
		return "", fmt.Errorf("Podotion image credential %s must be a single line", key)
	}
	if len([]byte(secret)) > MaxSecretBytes {
		return "", fmt.Errorf("Podotion image credential %s exceeds the 64 KB safety limit", key)
	}
	upper := strings.ToUpper(secret)
	normalized := regexp.MustCompile(`[^A-Z0-9]`).ReplaceAllString(upper, "")
	if secret == DefaultSecretMark || secret == FourKSecretMark ||
		(strings.HasPrefix(secret, "{{") && strings.HasSuffix(secret, "}}")) ||
		strings.Contains(normalized, "PODOTIONIMAGESK") ||
		strings.Contains(normalized, "PODOTIONIMAGE4KSK") {
		return "", fmt.Errorf("Podotion image credential %s placeholder has not been replaced", key)
	}
	return secret, nil
}

func ValidateModel(value string) (string, error) {
	model := strings.TrimSpace(value)
	if model == "" {
		return "", fmt.Errorf("model must not be empty")
	}
	if !modelPattern.MatchString(model) {
		return "", fmt.Errorf("model must be 1-128 letters, digits, dots, colons, slashes, underscores, or hyphens")
	}
	return model, nil
}

func ResolveModel(explicit string, config ModelSource) string {
	if strings.TrimSpace(explicit) != "" {
		return strings.TrimSpace(explicit)
	}
	if strings.TrimSpace(config.Model) != "" {
		return strings.TrimSpace(config.Model)
	}
	return DefaultModel
}

type ModelSource struct {
	Model string
}

func ReadExistingConfig(path string) (defaultSecret, fourKSecret, model string, err error) {
	cfg, err := LoadConfig(path)
	if err != nil {
		return "", "", "", err
	}
	return cfg.BearerToken, cfg.BearerToken4K, cfg.Model, nil
}

func RenderConfig(defaultSecret, fourKSecret, model string) (string, error) {
	defaultValue, err := validateSecret(defaultSecret, DefaultSecretKey)
	if err != nil {
		return "", err
	}
	fourKValue := ""
	if strings.TrimSpace(fourKSecret) != "" {
		fourKValue, err = validateSecret(fourKSecret, FourKSecretKey)
		if err != nil {
			return "", err
		}
	}
	modelValue := strings.TrimSpace(model)
	if modelValue != "" {
		modelValue, err = ValidateModel(modelValue)
		if err != nil {
			return "", err
		}
	}
	lines := []string{
		fmt.Sprintf("base_url = %s", strconv.Quote(DirectBaseURL)),
	}
	if modelValue != "" {
		lines = append(lines, fmt.Sprintf("model = %s", strconv.Quote(modelValue)))
	}
	lines = append(lines, fmt.Sprintf("%s = %s", DefaultSecretKey, strconv.Quote(defaultValue)))
	if fourKValue != "" {
		lines = append(lines, fmt.Sprintf("%s = %s", FourKSecretKey, strconv.Quote(fourKValue)))
	}
	return strings.Join(lines, "\n") + "\n", nil
}

func WriteConfig(path, defaultSecret, fourKSecret, model string) (string, error) {
	target, err := ConfigPath(path)
	if err != nil {
		return "", err
	}
	content, err := RenderConfig(defaultSecret, fourKSecret, model)
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(filepath.Dir(target), 0o700); err != nil {
		return "", fmt.Errorf("cannot create the credential directory: %w", err)
	}
	_ = os.Chmod(filepath.Dir(target), 0o700)
	temp, err := os.CreateTemp(filepath.Dir(target), ".provider.toml.*.tmp")
	if err != nil {
		return "", fmt.Errorf("cannot create the credential file: %w", err)
	}
	tempPath := temp.Name()
	defer os.Remove(tempPath)
	if err := temp.Chmod(0o600); err != nil && !os.IsPermission(err) {
		temp.Close()
		return "", err
	}
	if _, err := io.WriteString(temp, content); err != nil {
		temp.Close()
		return "", err
	}
	if err := temp.Sync(); err != nil {
		temp.Close()
		return "", err
	}
	if err := temp.Close(); err != nil {
		return "", err
	}
	if err := os.Rename(tempPath, target); err != nil {
		return "", fmt.Errorf("cannot replace the credential file: %w", err)
	}
	_ = os.Chmod(target, 0o600)
	return target, nil
}

func SetModel(path, model string) (map[string]any, error) {
	target, err := ConfigPath(path)
	if err != nil {
		return nil, err
	}
	defaultSecret, fourKSecret, _, err := ReadExistingConfig(target)
	if err != nil {
		return nil, err
	}
	model, err = ValidateModel(model)
	if err != nil {
		return nil, err
	}
	if _, err := WriteConfig(target, defaultSecret, fourKSecret, model); err != nil {
		return nil, err
	}
	return map[string]any{
		"ok":          true,
		"operation":   "set-model",
		"config_path": target,
		"model":       model,
		"credential_profiles": map[string]any{
			"default": true,
			"4k":      fourKSecret != "",
		},
	}, nil
}

func ConfigCheck(path string) (map[string]any, error) {
	target, err := ConfigPath(path)
	if err != nil {
		return nil, err
	}
	_, fourKSecret, model, err := ReadExistingConfig(target)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"ok":          true,
		"operation":   "check",
		"config_path": target,
		"model":       model,
		"credential_profiles": map[string]any{
			"default": true,
			"4k":      fourKSecret != "",
		},
	}, nil
}

func ParseCredentialInput(text string, set4k bool) (defaultSecret, fourKSecret string, err error) {
	if !utf8.ValidString(text) {
		return "", "", fmt.Errorf("credential input must be valid UTF-8")
	}
	normalized := strings.TrimPrefix(strings.ReplaceAll(text, "\r\n", "\n"), "\ufeff")
	lines := strings.Split(normalized, "\n")
	nonEmpty := make([]string, 0, len(lines))
	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		if trimmed != "" && !strings.HasPrefix(trimmed, "#") {
			nonEmpty = append(nonEmpty, trimmed)
		}
	}
	if !looksLikeCredentialAssignments(nonEmpty) {
		if set4k {
			if len(nonEmpty) != 1 {
				return "", "", fmt.Errorf("--set-4k stdin must contain exactly one 4K credential")
			}
			fourKSecret, err = validateSecret(nonEmpty[0], FourKSecretKey)
			return "", fourKSecret, err
		}
		if len(nonEmpty) == 0 || len(nonEmpty) > 2 {
			return "", "", fmt.Errorf("credential input must contain a default credential and an optional 4K credential")
		}
		defaultSecret, err = validateSecret(nonEmpty[0], DefaultSecretKey)
		if err != nil {
			return "", "", err
		}
		if len(nonEmpty) == 2 {
			fourKSecret, err = validateSecret(nonEmpty[1], FourKSecretKey)
			if err != nil {
				return "", "", err
			}
		}
		return defaultSecret, fourKSecret, nil
	}
	found := map[string]string{}
	for _, line := range nonEmpty {
		index := strings.Index(line, "=")
		if index <= 0 {
			return "", "", fmt.Errorf("credential assignments must use the exact Key=value format")
		}
		key := strings.TrimSpace(line[:index])
		value := strings.TrimSpace(line[index+1:])
		if key != DefaultSecretKey && key != FourKSecretKey {
			return "", "", fmt.Errorf("credential input contains an unknown key")
		}
		if _, exists := found[key]; exists {
			return "", "", fmt.Errorf("credential input contains a duplicate key")
		}
		found[key] = value
	}
	if set4k {
		if len(found) != 1 || found[FourKSecretKey] == "" {
			return "", "", fmt.Errorf("--set-4k input must contain exactly %s", FourKSecretKey)
		}
	} else if found[DefaultSecretKey] == "" {
		return "", "", fmt.Errorf("credential input must contain %s", DefaultSecretKey)
	}
	if value, ok := found[DefaultSecretKey]; ok {
		defaultSecret, err = validateSecret(value, DefaultSecretKey)
		if err != nil {
			return "", "", err
		}
	}
	if value, ok := found[FourKSecretKey]; ok {
		fourKSecret, err = validateSecret(value, FourKSecretKey)
		if err != nil {
			return "", "", err
		}
	}
	return defaultSecret, fourKSecret, nil
}

func looksLikeCredentialAssignments(lines []string) bool {
	for _, line := range lines {
		key, _, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		key = strings.TrimSpace(key)
		if key == DefaultSecretKey || key == FourKSecretKey || isIdentifier(key) {
			return true
		}
	}
	return false
}

func isIdentifier(value string) bool {
	if value == "" || !(value[0] == '_' || value[0] >= 'A' && value[0] <= 'Z' || value[0] >= 'a' && value[0] <= 'z') {
		return false
	}
	for _, character := range value[1:] {
		if character != '_' && !(character >= 'A' && character <= 'Z') && !(character >= 'a' && character <= 'z') && !(character >= '0' && character <= '9') {
			return false
		}
	}
	return true
}
func ReadCredentialFile(path string) (string, error) {
	info, err := os.Stat(path)
	if err != nil {
		if os.IsNotExist(err) {
			return "", fmt.Errorf("credential input file was not found")
		}
		return "", err
	}
	if info.Size() > 2*MaxSecretBytes+4096 {
		return "", fmt.Errorf("credential input file exceeds the safety limit")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("credential input file could not be read")
	}
	return string(raw), nil
}

func RandomSuffix() string {
	var buf [8]byte
	if _, err := rand.Read(buf[:]); err != nil {
		return strconv.FormatInt(time.Now().UnixNano(), 16)
	}
	return hex.EncodeToString(buf[:])
}
