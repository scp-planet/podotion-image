package podotion

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

type InputImageMeta struct {
	SHA256 string `json:"sha256"`
	Bytes  int64  `json:"bytes"`
}

type RecordImage struct {
	Path         string `json:"path"`
	MarkdownPath string `json:"markdown_path"`
	MimeType     string `json:"mime_type"`
	Bytes        int64  `json:"bytes"`
	Width        *int   `json:"width"`
	Height       *int   `json:"height"`
	SHA256       string `json:"sha256,omitempty"`
}

type RequestRecord struct {
	Version            int              `json:"version"`
	RequestKey         string           `json:"request_key"`
	Fingerprint        string           `json:"fingerprint"`
	FingerprintVersion int              `json:"fingerprint_version"`
	Operation          string           `json:"operation"`
	Model              string           `json:"model"`
	Endpoint           string           `json:"endpoint"`
	Size               string           `json:"size"`
	CredentialProfile  string           `json:"credential_profile"`
	OutputDir          string           `json:"output_dir"`
	StateScope         string           `json:"state_scope"`
	PromptSHA256       string           `json:"prompt_sha256"`
	InputImages        []InputImageMeta `json:"input_images"`
	Status             string           `json:"status"`
	ForceNew           bool             `json:"force_new"`
	CreatedAt          string           `json:"created_at"`
	UpdatedAt          string           `json:"updated_at"`
	SubmittedAt        string           `json:"submitted_at,omitempty"`
	CompletedAt        string           `json:"completed_at,omitempty"`
	UpstreamAttempts   int              `json:"upstream_attempts"`
	MayHaveBeenBilled  bool             `json:"may_have_been_billed"`
	Images             []RecordImage    `json:"images"`
	Warnings           []map[string]any `json:"warnings"`
	Failure            map[string]any   `json:"failure,omitempty"`
	LastStatePath      string           `json:"last_state_path,omitempty"`
	ReusedFrom         string           `json:"reused_from_request_key,omitempty"`
}

type fileLock interface {
	Release() error
}

func nowISO() string {
	return time.Now().UTC().Format(time.RFC3339Nano)
}

func SafeThreadID(scope string) string {
	value := strings.TrimSpace(scope)
	if value == "" {
		value = strings.TrimSpace(os.Getenv("CODEX_THREAD_ID"))
	}
	if value == "" {
		return "unscoped"
	}
	safe := regexp.MustCompile(`[^A-Za-z0-9_.-]+`).ReplaceAllString(value, "_")
	safe = strings.Trim(safe, "._")
	if safe == "" {
		safe = "unscoped"
	}
	if len(safe) > 96 {
		digest := sha256.Sum256([]byte(value))
		safe = safe[:80] + "-" + hex.EncodeToString(digest[:6])
	}
	return safe
}

func stateDir(outputDir, scope string) string {
	return filepath.Join(outputDir, ".state", SafeThreadID(scope))
}

func statePath(outputDir, scope string) string {
	return filepath.Join(stateDir(outputDir, scope), "last.json")
}

func normalizeRequestKey(value string) (string, error) {
	key := strings.TrimSpace(value)
	valid := regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{7,127}$`)
	if !valid.MatchString(key) {
		return "", fmt.Errorf("request key must be 8-128 ASCII letters, digits, dots, underscores, or hyphens")
	}
	return key, nil
}

func requestRecordPath(outputDir, requestKey, scope string) (string, error) {
	key, err := normalizeRequestKey(requestKey)
	if err != nil {
		return "", err
	}
	return filepath.Join(stateDir(outputDir, scope), "requests", key+".json"), nil
}

func requestLockPath(outputDir, fingerprint, scope string) string {
	return filepath.Join(stateDir(outputDir, scope), "locks", fingerprint+".lock")
}

func atomicWriteJSON(path string, value any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	raw, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	raw = append(raw, '\n')
	temp, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".*.tmp")
	if err != nil {
		return err
	}
	tempPath := temp.Name()
	defer os.Remove(tempPath)
	_ = temp.Chmod(0o600)
	if _, err := temp.Write(raw); err != nil {
		temp.Close()
		return err
	}
	if err := temp.Sync(); err != nil {
		temp.Close()
		return err
	}
	if err := temp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tempPath, path); err != nil {
		return err
	}
	_ = os.Chmod(path, 0o600)
	return nil
}

func readRequestRecord(path string) (*RequestRecord, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var record RequestRecord
	if err := json.Unmarshal(raw, &record); err != nil {
		return nil, fmt.Errorf("invalid request state: %s", path)
	}
	return &record, nil
}

func iterRequestRecords(outputDir, scope string) ([]struct {
	Path   string
	Record *RequestRecord
}, error) {
	dir := filepath.Join(stateDir(outputDir, scope), "requests")
	entries, err := os.ReadDir(dir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })
	out := make([]struct {
		Path   string
		Record *RequestRecord
	}, 0)
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		path := filepath.Join(dir, entry.Name())
		record, err := readRequestRecord(path)
		if err != nil {
			return nil, err
		}
		if record != nil {
			out = append(out, struct {
				Path   string
				Record *RequestRecord
			}{path, record})
		}
	}
	return out, nil
}

func RequestFingerprint(operation, endpoint, model, size, credentialProfile, outputDir, scope, prompt string, inputImages []string) (string, map[string]any, []InputImageMeta, error) {
	imageMetadata := make([]InputImageMeta, 0, len(inputImages))
	for _, path := range inputImages {
		digest, err := SHA256File(path)
		if err != nil {
			return "", nil, nil, err
		}
		info, err := os.Stat(path)
		if err != nil {
			return "", nil, nil, err
		}
		imageMetadata = append(imageMetadata, InputImageMeta{SHA256: digest, Bytes: info.Size()})
	}
	metadata := map[string]any{
		"fingerprint_version": 1,
		"operation":           operation,
		"model":               model,
		"endpoint":            endpoint,
		"size":                size,
		"credential_profile":  credentialProfile,
		"output_dir":          strings.ToLower(filepath.Clean(outputDir)),
		"state_scope":         scope,
		"prompt_sha256":       fmt.Sprintf("%x", sha256.Sum256([]byte(prompt))),
		"input_images":        imageMetadata,
	}
	raw, err := json.Marshal(metadata)
	if err != nil {
		return "", nil, nil, err
	}
	digest := sha256.Sum256(raw)
	return hex.EncodeToString(digest[:]), metadata, imageMetadata, nil
}

func recordSavedImages(record *RequestRecord) ([]SavedImage, bool) {
	if record == nil || len(record.Images) == 0 {
		return nil, false
	}
	images := make([]SavedImage, 0, len(record.Images))
	for _, value := range record.Images {
		path := value.Path
		info, err := os.Stat(path)
		if err != nil || info.IsDir() {
			return nil, false
		}
		if value.Bytes > 0 && info.Size() != value.Bytes {
			return nil, false
		}
		if value.SHA256 != "" {
			digest, err := SHA256File(path)
			if err != nil || digest != value.SHA256 {
				return nil, false
			}
		}
		images = append(images, SavedImage{
			Path:         path,
			MarkdownPath: value.MarkdownPath,
			MimeType:     value.MimeType,
			Bytes:        info.Size(),
			Width:        value.Width,
			Height:       value.Height,
			SHA256:       value.SHA256,
		})
	}
	return images, true
}

func isRecentSuccess(record *RequestRecord) bool {
	if record == nil || record.Status != "succeeded" {
		return false
	}
	if _, ok := recordSavedImages(record); !ok {
		return false
	}
	completed, err := time.Parse(time.RFC3339Nano, record.CompletedAt)
	if err != nil {
		return false
	}
	age := time.Since(completed)
	return age >= 0 && age <= RequestReuseSeconds*time.Second
}

func requestStateError(message, kind string, record *RequestRecord) *ProviderError {
	details := map[string]any{}
	if record != nil {
		details = map[string]any{
			"request_key":          record.RequestKey,
			"fingerprint":          record.Fingerprint,
			"status":               record.Status,
			"may_have_been_billed": record.MayHaveBeenBilled,
		}
	}
	return &ProviderError{Kind: kind, Message: message, Attempts: 0, Details: details}
}
func acquireRequestLock(path string) (fileLock, bool, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, false, err
	}
	handle, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, false, err
	}
	lock, acquired, err := lockHandle(handle)
	if err != nil {
		handle.Close()
		return nil, false, err
	}
	if !acquired {
		handle.Close()
		return nil, false, nil
	}
	return lock, true, nil
}

func GetRequestStatus(outputDir, requestKey, scope string) (map[string]any, error) {
	dir, err := filepath.Abs(outputDir)
	if err != nil {
		return nil, err
	}
	path, err := requestRecordPath(dir, requestKey, scope)
	if err != nil {
		return nil, err
	}
	record, err := readRequestRecord(path)
	if err != nil {
		return nil, err
	}
	if record == nil {
		return map[string]any{
			"ok":          true,
			"found":       false,
			"request_key": requestKey,
			"state_path":  path,
		}, nil
	}
	stored := record.Status
	effective := stored
	if (stored == "prepared" || stored == "submitted") && record.Fingerprint != "" {
		lock, acquired, err := acquireRequestLock(requestLockPath(dir, record.Fingerprint, scope))
		if err != nil {
			return nil, err
		}
		if acquired {
			defer lock.Release()
		}
		if !acquired {
			effective = "request_in_progress"
		} else if stored == "submitted" {
			effective = "outcome_unknown"
		}
	}
	recommendation := "do_not_resubmit"
	safeToRetry := false
	switch effective {
	case "request_in_progress":
		recommendation = "wait_and_check_the_same_request_key"
	case "succeeded":
		recommendation = "reuse_saved_images"
	case "outcome_unknown", "completed_unusable":
		recommendation = "ask_before_abandoning_possible_charge"
	case "prepared":
		recommendation = "resume_the_same_request_key"
		safeToRetry = true
	}
	return map[string]any{
		"ok":                   true,
		"found":                true,
		"request_key":          record.RequestKey,
		"stored_status":        stored,
		"effective_status":     effective,
		"may_have_been_billed": record.MayHaveBeenBilled,
		"safe_to_retry":        safeToRetry,
		"recommendation":       recommendation,
		"state_path":           path,
		"request":              record,
	}, nil
}

func AbandonRequest(outputDir, requestKey, scope string, acknowledged bool) (map[string]any, error) {
	if !acknowledged {
		return nil, fmt.Errorf("abandoning a request requires acknowledging a possible charge")
	}
	dir, err := filepath.Abs(outputDir)
	if err != nil {
		return nil, err
	}
	path, err := requestRecordPath(dir, requestKey, scope)
	if err != nil {
		return nil, err
	}
	record, err := readRequestRecord(path)
	if err != nil {
		return nil, err
	}
	if record == nil {
		return nil, fmt.Errorf("request state not found at %s", path)
	}
	if record.Fingerprint == "" {
		return nil, fmt.Errorf("invalid request state: %s", path)
	}
	lock, acquired, err := acquireRequestLock(requestLockPath(dir, record.Fingerprint, scope))
	if err != nil {
		return nil, err
	}
	if !acquired {
		return nil, requestStateError("request is still in progress and cannot be abandoned", "request_in_progress", record)
	}
	defer lock.Release()
	switch record.Status {
	case "succeeded":
		return nil, requestStateError("a successful request cannot be abandoned", "request_already_succeeded", record)
	case "abandoned":
		return map[string]any{"ok": true, "request_key": requestKey, "status": "abandoned", "state_path": path}, nil
	}
	record.Status = "abandoned"
	record.UpdatedAt = nowISO()
	if err := atomicWriteJSON(path, record); err != nil {
		return nil, err
	}
	return map[string]any{
		"ok":                   true,
		"request_key":          requestKey,
		"status":               "abandoned",
		"may_have_been_billed": record.MayHaveBeenBilled,
		"state_path":           path,
	}, nil
}

func WriteLastState(outputDir string, images []SavedImage, operation, model, size, scope string) (string, error) {
	if err := os.MkdirAll(outputDir, 0o755); err != nil {
		return "", err
	}
	path := statePath(outputDir, scope)
	records := make([]map[string]any, 0, len(images))
	for _, image := range images {
		records = append(records, image.AsMap())
	}
	state := map[string]any{
		"version":    1,
		"updated_at": nowISO(),
		"operation":  operation,
		"model":      model,
		"size":       size,
		"images":     records,
	}
	return path, atomicWriteJSON(path, state)
}

func ReadLastImage(outputDir, scope string) (string, error) {
	path := statePath(outputDir, scope)
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return "", fmt.Errorf("no previous generated image state found at %s", path)
		}
		return "", err
	}
	var state map[string]any
	if err := json.Unmarshal(raw, &state); err != nil {
		return "", fmt.Errorf("invalid previous image state: %s", path)
	}
	images, ok := state["images"].([]any)
	if !ok || len(images) == 0 {
		return "", fmt.Errorf("previous image state does not contain an image")
	}
	if len(images) != 1 {
		return "", fmt.Errorf("previous generation contains %d images; ask the user to choose one and pass --image", len(images))
	}
	item, ok := images[0].(map[string]any)
	if !ok {
		return "", fmt.Errorf("previous image state contains an invalid path")
	}
	value, ok := item["path"].(string)
	if !ok || strings.TrimSpace(value) == "" {
		return "", fmt.Errorf("previous image state contains an invalid path")
	}
	pathValue, err := filepath.Abs(value)
	if err != nil {
		return "", err
	}
	if err := ValidateInputFile(pathValue); err != nil {
		return "", err
	}
	return pathValue, nil
}

func requestFailure(err *ProviderError) map[string]any {
	if err == nil {
		return nil
	}
	out := map[string]any{
		"error_kind":  err.Kind,
		"http_status": nil,
		"request_id":  nil,
		"cf_ray":      nil,
		"attempts":    err.Attempts,
	}
	if err.HTTPStatus != nil {
		out["http_status"] = *err.HTTPStatus
	}
	if err.RequestID != "" {
		out["request_id"] = err.RequestID
	}
	if err.CFRay != "" {
		out["cf_ray"] = err.CFRay
	}
	return out
}

func newRequestRecord(operation, requestKey, fingerprint, model, endpoint, size, credentialProfile, outputDir, scope, prompt string, inputMetadata []InputImageMeta, forceNew bool) *RequestRecord {
	now := nowISO()
	return &RequestRecord{
		Version:            RequestRecordVersion,
		RequestKey:         requestKey,
		Fingerprint:        fingerprint,
		FingerprintVersion: 1,
		Operation:          operation,
		Model:              model,
		Endpoint:           endpoint,
		Size:               size,
		CredentialProfile:  credentialProfile,
		OutputDir:          outputDir,
		StateScope:         scope,
		PromptSHA256:       fmt.Sprintf("%x", sha256.Sum256([]byte(prompt))),
		InputImages:        inputMetadata,
		Status:             "prepared",
		ForceNew:           forceNew,
		CreatedAt:          now,
		UpdatedAt:          now,
		UpstreamAttempts:   0,
		MayHaveBeenBilled:  false,
		Images:             []RecordImage{},
		Warnings:           []map[string]any{},
	}
}

func recordImages(images []SavedImage) []RecordImage {
	out := make([]RecordImage, 0, len(images))
	for _, image := range images {
		out = append(out, RecordImage{
			Path:         image.Path,
			MarkdownPath: image.MarkdownPath,
			MimeType:     image.MimeType,
			Bytes:        image.Bytes,
			Width:        image.Width,
			Height:       image.Height,
			SHA256:       image.SHA256,
		})
	}
	return out
}
