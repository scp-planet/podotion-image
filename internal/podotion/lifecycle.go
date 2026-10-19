package podotion

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

const (
	SkillName                = "podotion-image"
	RepositoryURL            = "https://github.com/scp-planet/podotion-image"
	RepositoryBranch         = "main"
	MetadataFilename         = ".podotion-image-source.json"
	TransactionDirectory     = ".podotion-image-transactions"
	TrashDirectory           = ".podotion-image-trash"
	LegacyMarketplaceName    = "personal"
	LegacyMarketplaceSource  = "local"
	LegacyMarketplaceRelPath = "./plugins/podotion-image"
	MaxArchiveBytes          = 100 * 1024 * 1024
	MaxMarketplaceBytes      = 1024 * 1024
)

var repositoryArchiveURL = "https://codeload.github.com/scp-planet/podotion-image/zip/refs/heads/main"

var lifecycleHTTPClient = &http.Client{Timeout: 120 * time.Second}

var requiredSkillFiles = []string{
	"SKILL.md",
	"agents/openai.yaml",
	"runtime.json",
	"bin/podotion-image.cmd",
	"bin/podotion-image.ps1",
	"bin/podotion-image.sh",
}

type LifecycleOptions struct {
	CodexHome string
}

func ResolveCodexHome() (string, error) {
	configured := environmentValue("CODEX_HOME")
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("cannot locate the user home directory: %w", err)
	}
	home, _ = filepath.Abs(home)
	if strings.TrimSpace(configured) == "" {
		return filepath.Join(home, ".codex"), nil
	}
	value := strings.TrimSpace(configured)
	if value == "~" {
		return home, nil
	}
	if strings.HasPrefix(value, "~/") || strings.HasPrefix(value, `~\`) {
		return filepath.Abs(filepath.Join(home, value[2:]))
	}
	if strings.HasPrefix(value, "~") {
		return "", fmt.Errorf("named-user CODEX_HOME paths are not supported")
	}
	if !filepath.IsAbs(value) {
		return "", fmt.Errorf("CODEX_HOME must be an absolute native path")
	}
	return filepath.Abs(value)
}

func environmentValue(key string) string {
	wanted := strings.ToLower(key)
	for _, entry := range os.Environ() {
		name, value, ok := strings.Cut(entry, "=")
		if ok && strings.ToLower(name) == wanted {
			return value
		}
	}
	return ""
}

func ExpectedSkillRoot(codexHome string) string {
	return filepath.Join(codexHome, "skills", SkillName)
}

func CredentialPath(codexHome string) string {
	return filepath.Join(codexHome, DirectConfigDirectory, DirectConfigFilename)
}

func RunningSkillRoot(codexHome string) string {
	if executable, err := os.Executable(); err == nil {
		if resolved, resolveErr := filepath.EvalSymlinks(executable); resolveErr == nil {
			executable = resolved
		}
		binDir := filepath.Dir(executable)
		if strings.EqualFold(filepath.Base(binDir), "bin") {
			candidate := filepath.Dir(binDir)
			if _, err := os.Stat(filepath.Join(candidate, "SKILL.md")); err == nil {
				return candidate
			}
		}
	}
	return ExpectedSkillRoot(codexHome)
}

func hasRequiredSkillFiles(root string) bool {
	for _, relative := range requiredSkillFiles {
		info, err := os.Stat(filepath.Join(root, filepath.FromSlash(relative)))
		if err != nil || info.IsDir() {
			return false
		}
	}
	return true
}

func validateManagedSkill(root, codexHome string) (string, error) {
	resolved, err := filepath.Abs(root)
	if err != nil {
		return "", err
	}
	if info, statErr := os.Lstat(resolved); statErr != nil {
		if os.IsNotExist(statErr) {
			return "", fmt.Errorf("Skill is not installed at the managed location: %s", ExpectedSkillRoot(codexHome))
		}
		return "", statErr
	} else if info.Mode()&os.ModeSymlink != 0 {
		return "", fmt.Errorf("refusing to manage a symlinked Skill directory")
	}
	expected, _ := filepath.Abs(ExpectedSkillRoot(codexHome))
	if !samePath(resolved, expected) {
		return "", fmt.Errorf("Skill is not installed at the managed location: %s", expected)
	}
	if !hasRequiredSkillFiles(resolved) {
		return "", fmt.Errorf("managed Skill installation is incomplete")
	}
	return resolved, nil
}

func samePath(left, right string) bool {
	l, _ := filepath.Abs(left)
	r, _ := filepath.Abs(right)
	return strings.EqualFold(filepath.Clean(l), filepath.Clean(r))
}

func skillDigest(root string) (string, error) {
	type entry struct {
		relative string
		path     string
	}
	entries := make([]entry, 0)
	err := filepath.WalkDir(root, func(path string, item os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		relative, relErr := filepath.Rel(root, path)
		if relErr != nil {
			return relErr
		}
		relative = filepath.ToSlash(relative)
		if item.IsDir() {
			return nil
		}
		if item.Type()&os.ModeSymlink != 0 {
			return fmt.Errorf("Skill contains an unsupported symlink: %s", relative)
		}
		if filepath.Base(path) == MetadataFilename {
			return nil
		}
		entries = append(entries, entry{relative: relative, path: path})
		return nil
	})
	if err != nil {
		return "", err
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].relative < entries[j].relative })
	digest := sha256.New()
	length := make([]byte, 8)
	for _, item := range entries {
		binary.BigEndian.PutUint64(length, uint64(len(item.relative)))
		digest.Write(length)
		digest.Write([]byte(item.relative))
		data, err := os.ReadFile(item.path)
		if err != nil {
			return "", err
		}
		binary.BigEndian.PutUint64(length, uint64(len(data)))
		digest.Write(length)
		digest.Write(data)
	}
	return hex.EncodeToString(digest.Sum(nil)), nil
}

func readSkillMetadata(root string) map[string]any {
	raw, err := os.ReadFile(filepath.Join(root, MetadataFilename))
	if err != nil {
		return nil
	}
	var payload map[string]any
	if json.Unmarshal(raw, &payload) != nil {
		return nil
	}
	return payload
}

func legacyPluginSource(home string) string {
	return filepath.Join(home, "plugins", SkillName)
}

func legacyMarketplacePath(home string) string {
	return filepath.Join(home, ".agents", "plugins", "marketplace.json")
}

func inspectLegacyPlugin(home string) map[string]any {
	source := legacyPluginSource(home)
	marketplace := legacyMarketplacePath(home)
	sourceState := "absent"
	sourceUnsafe := false
	if info, err := os.Lstat(source); err == nil {
		switch {
		case info.Mode()&os.ModeSymlink != 0:
			sourceState = "symlink"
			sourceUnsafe = true
		case info.IsDir():
			sourceState = "directory"
		default:
			sourceState = "non-directory"
			sourceUnsafe = true
		}
	} else if !os.IsNotExist(err) {
		sourceState = "unreadable"
		sourceUnsafe = true
	}
	marketplaceState := "absent"
	registrationState := "absent"
	marketplaceUnsafe := false
	if info, err := os.Lstat(marketplace); err == nil {
		if info.Mode()&os.ModeSymlink != 0 {
			marketplaceState = "symlink"
			registrationState = "unknown"
			marketplaceUnsafe = true
		} else if info.IsDir() {
			marketplaceState = "non-file"
			registrationState = "unknown"
			marketplaceUnsafe = true
		} else {
			payload, readErr := readMarketplace(marketplace)
			if readErr != nil {
				marketplaceState = "unreadable"
				registrationState = "unknown"
				marketplaceUnsafe = true
			} else {
				marketplaceState = "present"
				plugins, pluginsOK := payload["plugins"].([]any)
				name, _ := payload["name"].(string)
				if !pluginsOK || strings.TrimSpace(name) == "" {
					marketplaceState = "invalid"
					registrationState = "unknown"
					marketplaceUnsafe = true
				} else {
					sameName := make([]map[string]any, 0)
					for _, item := range plugins {
						entry, ok := item.(map[string]any)
						if ok && entry["name"] == SkillName {
							sameName = append(sameName, entry)
						}
					}
					for _, entry := range sameName {
						if !legacySourceMatches(entry["source"]) {
							registrationState = "different-source"
							marketplaceUnsafe = true
							break
						}
					}
					if registrationState == "absent" && len(sameName) > 0 {
						registrationState = "owned"
					}
				}
			}
		}
	} else if !os.IsNotExist(err) {
		marketplaceState = "unreadable"
		registrationState = "unknown"
		marketplaceUnsafe = true
	}
	detected := sourceState != "absent" || registrationState != "absent" || marketplaceUnsafe
	safeToRemove := detected && !marketplaceUnsafe && !sourceUnsafe &&
		(sourceState == "absent" || sourceState == "directory") &&
		(registrationState == "absent" || registrationState == "owned")
	return map[string]any{
		"detected":       detected,
		"safe_to_remove": safeToRemove,
		"marketplace":    marketplaceState,
		"registration":   registrationState,
		"source":         sourceState,
	}
}

func legacySourceMatches(value any) bool {
	entry, ok := value.(map[string]any)
	if !ok {
		return false
	}
	return entry["source"] == LegacyMarketplaceSource && entry["path"] == LegacyMarketplaceRelPath
}

func readMarketplace(path string) (map[string]any, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	if info.Size() > MaxMarketplaceBytes {
		return nil, fmt.Errorf("legacy Marketplace file exceeds the safety limit")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, err
	}
	return payload, nil
}
func BuildStatus(root, codexHome string) map[string]any {
	expected := ExpectedSkillRoot(codexHome)
	resolved, _ := filepath.Abs(root)
	rootExists := false
	if info, err := os.Lstat(resolved); err == nil && info.IsDir() && info.Mode()&os.ModeSymlink == 0 {
		rootExists = true
	}
	managed := rootExists && samePath(resolved, expected) && hasRequiredSkillFiles(resolved)
	credential := CredentialPath(codexHome)
	_, credentialErr := os.Stat(credential)
	home, _ := os.UserHomeDir()
	return map[string]any{
		"ok":                  true,
		"operation":           "status",
		"skill":               SkillName,
		"runtime_version":     BinaryVersion,
		"installed":           managed,
		"managed_install":     managed,
		"skill_root":          resolved,
		"expected_skill_root": expected,
		"repository":          RepositoryURL,
		"branch":              RepositoryBranch,
		"source":              readSkillMetadata(resolved),
		"credential": map[string]any{
			"path":                   credential,
			"configured":             credentialErr == nil,
			"preserved_by_update":    true,
			"preserved_by_uninstall": true,
		},
		"legacy_plugin": inspectLegacyPlugin(home),
	}
}

func leaveDirectory(target, fallback string) func() {
	cwd, err := os.Getwd()
	if err != nil {
		return func() {}
	}
	relative, err := filepath.Rel(target, cwd)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) || filepath.IsAbs(relative) {
		return func() {}
	}
	destination := fallback
	if info, statErr := os.Stat(destination); statErr != nil || !info.IsDir() {
		destination = filepath.Dir(target)
	}
	if err := os.Chdir(destination); err != nil {
		return func() {}
	}
	return func() { _ = os.Chdir(cwd) }
}
func UpdateSkill(ctx context.Context, root, codexHome string, dryRun bool) (map[string]any, error) {
	target, err := validateManagedSkill(root, codexHome)
	if err != nil {
		return nil, err
	}
	archive, err := downloadRepositoryArchive(ctx)
	if err != nil {
		return nil, err
	}
	archiveDigest := sha256.Sum256(archive)
	if dryRun {
		temp, err := os.MkdirTemp("", "podotion-image-update-*")
		if err != nil {
			return nil, err
		}
		defer os.RemoveAll(temp)
		stage := filepath.Join(temp, SkillName)
		if err := extractSkillArchive(archive, stage); err != nil {
			return nil, err
		}
		oldDigest, err := skillDigest(target)
		if err != nil {
			return nil, err
		}
		newDigest, err := skillDigest(stage)
		if err != nil {
			return nil, err
		}
		return updateResult(target, oldDigest, newDigest, archiveDigest, true, oldDigest != newDigest), nil
	}

	transactionRoot := filepath.Join(codexHome, TransactionDirectory)
	if err := os.MkdirAll(transactionRoot, 0o700); err != nil {
		return nil, fmt.Errorf("cannot create the update transaction directory: %w", err)
	}
	transaction := filepath.Join(transactionRoot, RandomSuffix())
	stage := filepath.Join(transaction, "stage", SkillName)
	backup := filepath.Join(transaction, "backup", SkillName)
	if err := os.MkdirAll(filepath.Dir(stage), 0o700); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Dir(backup), 0o700); err != nil {
		return nil, err
	}
	defer os.RemoveAll(transaction)
	if err := extractSkillArchive(archive, stage); err != nil {
		return nil, err
	}
	oldDigest, err := skillDigest(target)
	if err != nil {
		return nil, err
	}
	newDigest, err := skillDigest(stage)
	if err != nil {
		return nil, err
	}
	metadata := map[string]any{
		"schema":         1,
		"repository":     RepositoryURL,
		"branch":         RepositoryBranch,
		"archive_sha256": hex.EncodeToString(archiveDigest[:]),
		"content_sha256": newDigest,
		"updated_at":     time.Now().UTC().Format(time.RFC3339Nano),
	}
	if err := atomicWriteJSON(filepath.Join(stage, MetadataFilename), metadata); err != nil {
		return nil, err
	}
	if oldDigest == newDigest {
		if err := atomicWriteJSON(filepath.Join(target, MetadataFilename), metadata); err != nil {
			return nil, err
		}
		return updateResult(target, oldDigest, newDigest, archiveDigest, false, false), nil
	}
	restoreDirectory := leaveDirectory(target, codexHome)
	defer restoreDirectory()
	oldMoved := false
	newInstalled := false
	if err := os.Rename(target, backup); err != nil {
		return nil, fmt.Errorf("Skill update could not move the previous installation: %w", err)
	}
	oldMoved = true
	if err := os.Rename(stage, target); err != nil {
		_ = os.Rename(backup, target)
		oldMoved = false
		return nil, fmt.Errorf("Skill update failed; the previous installation was restored: %w", err)
	}
	newInstalled = true
	if !oldMoved || !newInstalled {
		return nil, fmt.Errorf("Skill update failed unexpectedly")
	}
	result := updateResult(target, oldDigest, newDigest, archiveDigest, false, true)
	result["updated"] = true
	return result, nil
}

func updateResult(target, oldDigest, newDigest string, archiveDigest [32]byte, dryRun, wouldUpdate bool) map[string]any {
	result := map[string]any{
		"ok":                         true,
		"operation":                  "update",
		"skill_root":                 target,
		"repository":                 RepositoryURL,
		"branch":                     RepositoryBranch,
		"archive_sha256":             hex.EncodeToString(archiveDigest[:]),
		"previous_content_sha256":    oldDigest,
		"content_sha256":             newDigest,
		"credential_preserved":       true,
		"generated_images_preserved": true,
		"updated":                    false,
	}
	if dryRun {
		result["dry_run"] = true
		result["would_update"] = wouldUpdate
	} else {
		result["updated"] = wouldUpdate
	}
	return result
}

func downloadRepositoryArchive(ctx context.Context) ([]byte, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, repositoryArchiveURL, nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Accept", "application/zip")
	request.Header.Set("User-Agent", "podotion-image-skill/"+BinaryVersion)
	response, err := lifecycleHTTPClient.Do(request)
	if err != nil {
		return nil, fmt.Errorf("Skill update download failed: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, fmt.Errorf("Skill update download failed with HTTP %d", response.StatusCode)
	}
	data, err := readLimited(response.Body, MaxArchiveBytes)
	if err != nil {
		return nil, fmt.Errorf("Skill update archive could not be read: %w", err)
	}
	return data, nil
}

func UninstallSkill(root, codexHome string, confirmed bool) (map[string]any, error) {
	target, err := validateManagedSkill(root, codexHome)
	if err != nil {
		return nil, err
	}
	if !confirmed {
		return nil, fmt.Errorf("uninstall requires --yes; credentials, generated images, and request state are preserved")
	}
	trashRoot := filepath.Join(codexHome, TrashDirectory)
	if err := os.MkdirAll(trashRoot, 0o700); err != nil {
		return nil, fmt.Errorf("cannot create the uninstall trash directory: %w", err)
	}
	tombstone := filepath.Join(trashRoot, RandomSuffix())
	restoreDirectory := leaveDirectory(target, codexHome)
	defer restoreDirectory()
	if err := os.Rename(target, tombstone); err != nil {
		return nil, fmt.Errorf("Skill uninstall could not detach the installed directory: %w", err)
	}
	warning := ""
	if err := os.RemoveAll(tombstone); err != nil {
		warning = "Skill is uninstalled but cleanup remains at: " + tombstone
	}
	result := map[string]any{
		"ok":                         true,
		"operation":                  "uninstall",
		"uninstalled":                true,
		"skill_root":                 target,
		"credential":                 map[string]any{"path": CredentialPath(codexHome), "preserved": true},
		"generated_images_preserved": true,
		"request_state_preserved":    true,
	}
	if warning != "" {
		result["warning"] = warning
	}
	return result, nil
}

func UninstallLegacyPlugin(codexHome string, confirmed bool) (map[string]any, error) {
	if !confirmed {
		return nil, fmt.Errorf("legacy Plugin uninstall requires --yes; credentials, images, and request state are preserved")
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, err
	}
	home, _ = filepath.Abs(home)
	inspection := inspectLegacyPlugin(home)
	detected, _ := inspection["detected"].(bool)
	if !detected {
		return map[string]any{
			"ok":                         true,
			"operation":                  "uninstall-legacy-plugin",
			"legacy_plugin_found":        false,
			"uninstalled":                false,
			"credential_preserved":       true,
			"generated_images_preserved": true,
			"request_state_preserved":    true,
			"restart_required":           false,
		}, nil
	}
	safe, _ := inspection["safe_to_remove"].(bool)
	if !safe {
		return nil, fmt.Errorf("legacy Plugin installation cannot be migrated safely; inspect the status output before retrying")
	}
	marketplace := legacyMarketplacePath(home)
	source := legacyPluginSource(home)
	warnings := []string{}
	marketplaceRemoved := false
	if _, statErr := os.Stat(marketplace); statErr == nil {
		payload, readErr := readMarketplace(marketplace)
		if readErr != nil {
			return nil, fmt.Errorf("legacy Marketplace file could not be read safely")
		}
		plugins, _ := payload["plugins"].([]any)
		retained := make([]any, 0, len(plugins))
		for _, item := range plugins {
			entry, ok := item.(map[string]any)
			if ok && entry["name"] == SkillName && legacySourceMatches(entry["source"]) {
				marketplaceRemoved = true
				continue
			}
			retained = append(retained, item)
		}
		if marketplaceRemoved {
			payload["plugins"] = retained
			if writeErr := atomicWriteJSON(marketplace, payload); writeErr != nil {
				return nil, fmt.Errorf("legacy Marketplace registration could not be removed: %w", writeErr)
			}
		}
	}
	sourceRemoved := false
	if info, statErr := os.Lstat(source); statErr == nil {
		if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
			return nil, fmt.Errorf("legacy Plugin source changed during migration and was not removed")
		}
		if removeErr := os.RemoveAll(source); removeErr != nil {
			warnings = append(warnings, "legacy Plugin source cleanup is incomplete: "+source)
		} else {
			sourceRemoved = true
		}
	} else if !os.IsNotExist(statErr) {
		warnings = append(warnings, "legacy Plugin source could not be inspected")
	}
	result := map[string]any{
		"ok":                         true,
		"operation":                  "uninstall-legacy-plugin",
		"legacy_plugin_found":        true,
		"uninstalled":                true,
		"plugin_id":                  SkillName + "@" + LegacyMarketplaceName,
		"marketplace_entry_removed":  marketplaceRemoved,
		"plugin_source_removed":      sourceRemoved,
		"credential_preserved":       true,
		"generated_images_preserved": true,
		"request_state_preserved":    true,
		"restart_required":           true,
	}
	if len(warnings) > 0 {
		result["warnings"] = warnings
	}
	return result, nil
}
