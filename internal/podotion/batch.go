package podotion

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

const MaxBatchJobs = 10

var ErrBatchFailed = errors.New("one or more batch jobs failed; successful images were preserved")

type BatchManifest struct {
	Schema  int        `json:"schema"`
	BatchID string     `json:"batch_id"`
	Jobs    []BatchJob `json:"jobs"`
}

type BatchJob struct {
	JobID     string   `json:"job_id"`
	Operation string   `json:"operation"`
	Prompt    string   `json:"prompt"`
	Model     string   `json:"model,omitempty"`
	Size      string   `json:"size,omitempty"`
	Ratio     string   `json:"ratio,omitempty"`
	Images    []string `json:"images,omitempty"`
}

type BatchOptions struct {
	InputFile      string
	OutputDir      string
	StateScope     string
	Concurrency    int
	CredentialFile string
	Model          string
}

type frozenInput struct {
	File     string         `json:"file"`
	SHA256   string         `json:"sha256"`
	MIME     string         `json:"mime"`
	Suffix   string         `json:"suffix"`
	Original InputImageMeta `json:"original"`
}

// No provider credentials are written into a batch snapshot.
type frozenJob struct {
	JobID         string           `json:"job_id"`
	Operation     string           `json:"operation"`
	Prompt        string           `json:"prompt"`
	Model         string           `json:"model"`
	Size          ResolvedSize     `json:"size"`
	RequestKey    string           `json:"request_key"`
	Fingerprint   string           `json:"fingerprint"`
	OriginalPaths []string         `json:"original_paths"`
	Inputs        []frozenInput    `json:"inputs"`
	Warnings      []map[string]any `json:"warnings"`
}

type batchSnapshot struct {
	Schema         int         `json:"schema"`
	BatchID        string      `json:"batch_id"`
	ManifestSHA256 string      `json:"manifest_sha256"`
	Jobs           []frozenJob `json:"jobs"`
}

func digestText(value string) string { return fmt.Sprintf("%x", sha256.Sum256([]byte(value))) }

func batchJobKey(batchID, jobID string) string {
	return "batch-" + digestText(batchID+"\x00"+jobID)
}

func readBatchManifest(path string) (BatchManifest, error) {
	var manifest BatchManifest
	handle, err := os.Open(path)
	if err != nil {
		return manifest, err
	}
	defer handle.Close()
	raw, err := io.ReadAll(io.LimitReader(handle, MaxBatchJobs*MaxPromptBytes+65537))
	if err != nil {
		return manifest, err
	}
	if len(raw) > MaxBatchJobs*MaxPromptBytes+65536 {
		return manifest, fmt.Errorf("batch manifest is too large")
	}
	decoder := json.NewDecoder(bytes.NewReader(bytes.TrimPrefix(raw, []byte{0xef, 0xbb, 0xbf})))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		return manifest, fmt.Errorf("invalid batch manifest: %w", err)
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return manifest, fmt.Errorf("batch manifest must contain one JSON object")
	}
	identifier := regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`)
	if manifest.Schema != 1 || !identifier.MatchString(manifest.BatchID) {
		return manifest, fmt.Errorf("batch requires schema=1 and a 1-128 character ASCII batch_id")
	}
	if len(manifest.Jobs) < 1 || len(manifest.Jobs) > MaxBatchJobs {
		return manifest, fmt.Errorf("batch requires 1-%d jobs", MaxBatchJobs)
	}
	seen := map[string]bool{}
	for index := range manifest.Jobs {
		job := &manifest.Jobs[index]
		if !identifier.MatchString(job.JobID) || seen[job.JobID] {
			return manifest, fmt.Errorf("job %d requires a unique 1-128 character ASCII job_id", index+1)
		}
		seen[job.JobID] = true
		if job.Operation != "generate" && job.Operation != "edit" {
			return manifest, fmt.Errorf("job %s: operation must be generate or edit", job.JobID)
		}
		for i, imagePath := range job.Images {
			if strings.TrimSpace(imagePath) == "" {
				return manifest, fmt.Errorf("job %s: empty image path", job.JobID)
			}
			if !filepath.IsAbs(imagePath) {
				imagePath = filepath.Join(filepath.Dir(path), imagePath)
			}
			job.Images[i], err = filepath.Abs(imagePath)
			if err != nil {
				return manifest, err
			}
		}
	}
	return manifest, nil
}

// RunBatch preflights and freezes every job before starting workers. The callback
// is invoked serially on this goroutine, as soon as each independent job ends.
func RunBatch(ctx context.Context, opts BatchOptions, emit func(map[string]any) error) (map[string]any, error) {
	if !filepath.IsAbs(opts.OutputDir) || strings.TrimSpace(opts.StateScope) == "" {
		return nil, fmt.Errorf("batch requires an absolute --output-dir and a nonempty --state-scope")
	}
	if opts.Concurrency < 0 || opts.Concurrency > MaxBatchJobs {
		return nil, fmt.Errorf("concurrency must be 1-%d", MaxBatchJobs)
	}
	manifest, err := readBatchManifest(opts.InputFile)
	if err != nil {
		return nil, err
	}
	concurrency := opts.Concurrency
	if concurrency == 0 {
		concurrency = len(manifest.Jobs)
	}
	outputDir := filepath.Clean(opts.OutputDir)
	scope := SafeThreadID(opts.StateScope)
	// A batch ID cannot be reused with changed prompts/options, including jobs
	// removed from a previously submitted batch. Defaults remain frozen on resume.
	raw, _ := json.Marshal(struct {
		Manifest BatchManifest
		Model    string
	}{manifest, opts.Model})
	manifestHash := fmt.Sprintf("%x", sha256.Sum256(raw))
	dir := filepath.Join(stateDir(outputDir, scope), "batches", digestText(manifest.BatchID))
	lock, acquired, err := acquireRequestLock(requestLockPath(outputDir, "batch-"+digestText(manifest.BatchID), scope))
	if err != nil {
		return nil, err
	}
	if !acquired {
		return nil, fmt.Errorf("this batch is in progress; do not submit it again")
	}
	defer lock.Release()
	config, err := loadConfig(opts.CredentialFile)
	if err != nil {
		return nil, err
	}
	prepared, err := prepareBatch(manifest, manifestHash, dir, outputDir, scope, opts.Model, config)
	if err != nil {
		return nil, err
	}
	type completion struct {
		index  int
		result map[string]any
		err    error
	}
	work := make(chan int, len(prepared))
	done := make(chan completion, len(prepared))
	for i := range prepared {
		work <- i
	}
	close(work)
	for worker := 0; worker < concurrency && worker < len(prepared); worker++ {
		go func() {
			for i := range work {
				result, jobErr := runPreparedGeneration(ctx, prepared[i])
				done <- completion{i, result, jobErr}
			}
		}()
	}
	ordered := make([]map[string]any, len(prepared))
	failed, succeeded := 0, 0
	var streamErr error
	for range prepared {
		completed := <-done
		result := completed.result
		if completed.err != nil {
			failed++
			result = map[string]any{"ok": false, "operation": prepared[completed.index].opts.Operation, "model": prepared[completed.index].opts.Model, "request": map[string]any{"request_key": prepared[completed.index].opts.RequestKey}, "error": batchError(completed.err, config.SecretValues())}
		} else {
			succeeded++
		}
		event := map[string]any{"event": "job_completed", "schema": 1, "batch_id": manifest.BatchID, "index": completed.index + 1, "job_id": manifest.Jobs[completed.index].JobID, "result": result}
		ordered[completed.index] = event
		if emit != nil {
			if err := emit(event); err != nil && streamErr == nil {
				streamErr = err
			}
		}
	}
	// Release upload snapshots held in memory before verifying saved outputs.
	for _, p := range prepared {
		p.inputs = nil
	}
	// Workers write only their private last state. Publish the parent once, in
	// manifest order, regardless of network completion order.
	allImages := []SavedImage{}
	for _, p := range prepared {
		path, _ := requestRecordPath(outputDir, p.opts.RequestKey, scope)
		record, readErr := readRequestRecord(path)
		if readErr == nil && record != nil && record.Status == "succeeded" {
			if images, ok := recordSavedImages(record); ok {
				allImages = append(allImages, images...)
			}
		}
	}
	stateFile := ""
	if len(allImages) > 0 {
		stateFile, err = WriteLastState(outputDir, allImages, "batch", "", "", scope)
	}
	summary := map[string]any{"event": "batch_completed", "schema": 1, "ok": failed == 0 && err == nil && streamErr == nil, "operation": "batch", "batch_id": manifest.BatchID, "jobs": ordered, "succeeded": succeeded, "failed": failed, "state_path": nilIfEmpty(stateFile), "snapshot_path": filepath.Join(dir, "snapshot.json")}
	if err != nil {
		summary["error"] = batchError(err, config.SecretValues())
		return summary, err
	}
	if streamErr != nil {
		return summary, streamErr
	}
	if failed > 0 {
		return summary, ErrBatchFailed
	}
	return summary, nil
}

func batchError(err error, secrets []string) map[string]any {
	if providerErr, ok := err.(*ProviderError); ok {
		return SanitizeValue(providerErr.AsMap(), secrets...).(map[string]any)
	}
	return map[string]any{"type": "LocalError", "message": RedactSecrets(err.Error(), secrets...), "attempts": 0, "may_have_been_billed": false}
}

func prepareBatch(manifest BatchManifest, manifestHash, dir, outputDir, scope, overrideModel string, config ProviderConfig) ([]*preparedGeneration, error) {
	path := filepath.Join(dir, "snapshot.json")
	raw, err := os.ReadFile(path)
	if err == nil {
		var snapshot batchSnapshot
		if json.Unmarshal(raw, &snapshot) != nil || snapshot.Schema != 1 || snapshot.BatchID != manifest.BatchID || snapshot.ManifestSHA256 != manifestHash || len(snapshot.Jobs) != len(manifest.Jobs) {
			return nil, fmt.Errorf("batch_id already has a different or invalid snapshot; use the original manifest")
		}
		return restoreBatch(snapshot, dir, outputDir, scope, config)
	}
	if !os.IsNotExist(err) {
		return nil, err
	}
	prepared := make([]*preparedGeneration, 0, len(manifest.Jobs))
	for _, job := range manifest.Jobs {
		model := job.Model
		if model == "" {
			model = overrideModel
		}
		p, err := prepareGeneration(GenerateOptions{Operation: job.Operation, Prompt: job.Prompt, Model: model, Size: job.Size, Ratio: job.Ratio, InputImages: job.Images, OutputDir: outputDir, StateScope: scope, RequestKey: batchJobKey(manifest.BatchID, job.JobID)}, &config)
		if err != nil {
			return nil, fmt.Errorf("job %s preflight: %w", job.JobID, err)
		}
		p.fingerprint = digestText("batch-v1\x00" + p.opts.RequestKey + "\x00" + p.fingerprint)
		p.batchID, p.jobID = manifest.BatchID, job.JobID
		p.lastScope = SafeThreadID(scope + ".job." + p.opts.RequestKey)
		prepared = append(prepared, p)
	}
	// Nothing above creates a submitted request. Persist the entire immutable
	// input set before launching even the first worker.
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	snapshot := batchSnapshot{Schema: 1, BatchID: manifest.BatchID, ManifestSHA256: manifestHash}
	for i, p := range prepared {
		frozen := frozenJob{JobID: manifest.Jobs[i].JobID, Operation: p.opts.Operation, Prompt: p.opts.Prompt, Model: p.opts.Model, Size: p.size, RequestKey: p.opts.RequestKey, Fingerprint: p.fingerprint, OriginalPaths: p.opts.InputImages, Warnings: p.warnings}
		for j, input := range p.inputs {
			name := fmt.Sprintf("job-%02d-image-%02d%s", i+1, j+1, input.Suffix)
			if err := writeSnapshotBytes(filepath.Join(dir, name), input.Data); err != nil {
				return nil, err
			}
			frozen.Inputs = append(frozen.Inputs, frozenInput{File: name, SHA256: fmt.Sprintf("%x", sha256.Sum256(input.Data)), MIME: input.MIME, Suffix: input.Suffix, Original: input.Original})
		}
		snapshot.Jobs = append(snapshot.Jobs, frozen)
	}
	if err := atomicWriteJSON(path, snapshot); err != nil {
		return nil, err
	}
	return prepared, nil
}

func restoreBatch(snapshot batchSnapshot, dir, outputDir, scope string, config ProviderConfig) ([]*preparedGeneration, error) {
	prepared := make([]*preparedGeneration, 0, len(snapshot.Jobs))
	for _, job := range snapshot.Jobs {
		if job.RequestKey != batchJobKey(snapshot.BatchID, job.JobID) {
			return nil, fmt.Errorf("invalid batch snapshot job identity")
		}
		config.Model = job.Model
		provider, err := config.SelectCredential(job.Size.CredentialProfile())
		if err != nil {
			return nil, err
		}
		endpoint, err := BuildImagesURL(provider.BaseURL, job.Operation)
		if err != nil {
			return nil, err
		}
		p := &preparedGeneration{opts: GenerateOptions{Operation: job.Operation, Prompt: job.Prompt, Model: job.Model, Size: job.Size.Value, OutputDir: outputDir, StateScope: scope, RequestKey: job.RequestKey, InputImages: job.OriginalPaths}, size: job.Size, provider: provider, endpoint: endpoint, fingerprint: job.Fingerprint, warnings: job.Warnings, lastScope: SafeThreadID(scope + ".job." + job.RequestKey)}
		p.batchID, p.jobID = snapshot.BatchID, job.JobID
		p.metadata = []InputImageMeta{}
		for _, input := range job.Inputs {
			if filepath.Base(input.File) != input.File {
				return nil, fmt.Errorf("invalid snapshot input path")
			}
			handle, err := os.Open(filepath.Join(dir, input.File))
			if err != nil {
				return nil, fmt.Errorf("batch snapshot input unavailable: %w", err)
			}
			data, err := io.ReadAll(io.LimitReader(handle, MaxInputFileBytes+1))
			handle.Close()
			if err != nil || len(data) > MaxInputFileBytes || fmt.Sprintf("%x", sha256.Sum256(data)) != input.SHA256 {
				return nil, fmt.Errorf("batch snapshot input changed; do not resubmit with new inputs")
			}
			validated, err := prepareInputImage(data, len(p.inputs)+1)
			if err != nil || validated.MIME != input.MIME || validated.Suffix != input.Suffix {
				return nil, fmt.Errorf("invalid batch snapshot image format")
			}
			p.inputs = append(p.inputs, uploadImage{Data: data, MIME: input.MIME, Suffix: input.Suffix, Original: input.Original})
			p.metadata = append(p.metadata, input.Original)
		}
		fingerprint, _, _, err := fingerprintFromMetadata(job.Operation, endpoint, job.Model, job.Size.Value, provider.CredentialProfile, outputDir, scope, job.Prompt, p.metadata)
		if err != nil || digestText("batch-v1\x00"+job.RequestKey+"\x00"+fingerprint) != job.Fingerprint {
			return nil, fmt.Errorf("batch snapshot options changed; do not resubmit")
		}
		prepared = append(prepared, p)
	}
	return prepared, nil
}

func writeSnapshotBytes(path string, data []byte) error {
	temp, err := os.CreateTemp(filepath.Dir(path), ".input-*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(temp.Name())
	if _, err := temp.Write(data); err != nil {
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
	return os.Rename(temp.Name(), path)
}
