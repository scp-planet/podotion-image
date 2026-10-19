package podotion

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type GenerateOptions struct {
	CredentialFile string
	Model          string
	Operation      string
	Prompt         string
	Size           string
	Ratio          string
	OutputDir      string
	RequestKey     string
	ForceNew       bool
	StateScope     string
	InputImages    []string
	UseLast        bool
}

var loadConfig = LoadConfig

func RunGeneration(ctx context.Context, opts GenerateOptions) (map[string]any, error) {
	started := time.Now()
	resolvedSize, err := ResolveRequestSize(opts.Size, opts.Ratio)
	if err != nil {
		return nil, fmt.Errorf("%s", err)
	}
	prompt, err := normalizePrompt(opts.Prompt)
	if err != nil {
		return nil, err
	}
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
	provider, err := config.SelectCredential(resolvedSize.CredentialProfile())
	if err != nil {
		return nil, err
	}
	requestKey, err := normalizeRequestKey(opts.RequestKey)
	if err != nil {
		return nil, err
	}
	outputDir, err := ResolveOutputDir(opts.OutputDir)
	if err != nil {
		return nil, err
	}
	scopeValue := SafeThreadID(opts.StateScope)
	inputImages, err := resolveInputImages(opts.Operation, opts.InputImages, opts.UseLast, outputDir, scopeValue)
	if err != nil {
		return nil, err
	}
	endpoint, err := BuildImagesURL(provider.BaseURL, opts.Operation)
	if err != nil {
		return nil, err
	}
	fingerprint, _, imageMetadata, err := RequestFingerprint(
		opts.Operation, endpoint, model, resolvedSize.Value, provider.CredentialProfile,
		outputDir, scopeValue, prompt, inputImages,
	)
	if err != nil {
		return nil, err
	}
	recordPath, err := requestRecordPath(outputDir, requestKey, scopeValue)
	if err != nil {
		return nil, err
	}
	lockPath := requestLockPath(outputDir, fingerprint, scopeValue)
	lock, acquired, err := acquireRequestLock(lockPath)
	if err != nil {
		return nil, err
	}
	if !acquired {
		var active *RequestRecord
		records, _ := iterRequestRecords(outputDir, scopeValue)
		for _, entry := range records {
			if entry.Record.Fingerprint == fingerprint && (entry.Record.Status == "prepared" || entry.Record.Status == "submitted") {
				active = entry.Record
				break
			}
		}
		if active == nil {
			active = &RequestRecord{RequestKey: requestKey, Fingerprint: fingerprint, Status: "submitted", MayHaveBeenBilled: true}
		}
		return nil, requestStateError("an equivalent image request is still in progress; check its status instead of resubmitting", "request_in_progress", active)
	}
	defer lock.Release()

	existing, err := readRequestRecord(recordPath)
	if err != nil {
		return nil, err
	}
	if existing != nil && existing.Fingerprint != fingerprint {
		return nil, requestStateError("request key was already used for different image inputs", "request_key_conflict", existing)
	}
	if existing != nil {
		switch existing.Status {
		case "succeeded":
			images, ok := recordSavedImages(existing)
			if !ok {
				existing.Status = "completed_unusable"
				existing.UpdatedAt = nowISO()
				existing.Failure = map[string]any{"error_kind": "request_result_missing"}
				_ = atomicWriteJSON(recordPath, existing)
				return nil, requestStateError("saved images for this request are missing or changed; the request cannot be sent again", "request_result_missing", existing)
			}
			return generationResult(existing, provider, endpoint, resolvedSize, inputImages, images, existing.Warnings, true, existing.ReusedFrom, existing.LastStatePath), nil
		case "prepared":
			// Resume the prepared request using the same durable identity.
		case "submitted":
			existing.Status = "outcome_unknown"
			existing.UpdatedAt = nowISO()
			existing.MayHaveBeenBilled = true
			existing.Failure = map[string]any{"error_kind": "interrupted_after_submission", "message": "the prior process ended after submission without a durable result"}
			_ = atomicWriteJSON(recordPath, existing)
			return nil, requestStateError("the prior request outcome is unknown; do not resubmit it automatically", "request_outcome_unknown", existing)
		default:
			return nil, requestStateError("request key has already reached a terminal state and cannot send another upstream request", "request_"+existing.Status, existing)
		}
	}

	records, err := iterRequestRecords(outputDir, scopeValue)
	if err != nil {
		return nil, err
	}
	for _, entry := range records {
		if entry.Path == recordPath || entry.Record.Fingerprint != fingerprint {
			continue
		}
		prior := entry.Record
		if prior.Status == "submitted" {
			prior.Status = "outcome_unknown"
			prior.UpdatedAt = nowISO()
			prior.MayHaveBeenBilled = true
			_ = atomicWriteJSON(entry.Path, prior)
		}
		if prior.Status == "outcome_unknown" || prior.Status == "completed_unusable" {
			return nil, requestStateError("an equivalent request may already have been charged; explicitly abandon it before creating another", "request_outcome_unknown", prior)
		}
		if prior.Status == "succeeded" && isRecentSuccess(prior) && !opts.ForceNew {
			alias := *prior
			alias.RequestKey = requestKey
			alias.CreatedAt = nowISO()
			alias.UpdatedAt = alias.CreatedAt
			alias.UpstreamAttempts = 0
			alias.MayHaveBeenBilled = false
			alias.ReusedFrom = prior.RequestKey
			if err := atomicWriteJSON(recordPath, &alias); err != nil {
				return nil, err
			}
			images, _ := recordSavedImages(&alias)
			return generationResult(&alias, provider, endpoint, resolvedSize, inputImages, images, alias.Warnings, true, prior.RequestKey, alias.LastStatePath), nil
		}
	}

	record := newRequestRecord(opts.Operation, requestKey, fingerprint, model, endpoint, resolvedSize.Value, provider.CredentialProfile, outputDir, scopeValue, prompt, imageMetadata, opts.ForceNew)
	if err := atomicWriteJSON(recordPath, record); err != nil {
		return nil, err
	}
	record.Status = "submitted"
	record.UpdatedAt = nowISO()
	record.SubmittedAt = record.UpdatedAt
	record.UpstreamAttempts = 1
	record.MayHaveBeenBilled = true
	if err := atomicWriteJSON(recordPath, record); err != nil {
		return nil, err
	}

	response, err := PostImages(ctx, provider, opts.Operation, prompt, inputImages, resolvedSize.Value)
	if err != nil {
		providerErr := asProviderError(err)
		status := "outcome_unknown"
		if providerErr.Kind == "output_decode_error" {
			status = "completed_unusable"
		} else if providerErr.HTTPStatus != nil && *providerErr.HTTPStatus >= 400 && *providerErr.HTTPStatus < 500 {
			status = "failed_definitive"
		}
		record.Status = status
		record.UpdatedAt = nowISO()
		record.CompletedAt = record.UpdatedAt
		record.Failure = requestFailure(providerErr)
		_ = atomicWriteJSON(recordPath, record)
		providerErr.Details["request_key"] = requestKey
		providerErr.Details["fingerprint"] = fingerprint
		providerErr.Details["status"] = status
		providerErr.Details["may_have_been_billed"] = true
		return nil, providerErr
	}

	results, err := ExtractImageResults(response)
	if err == nil {
		var saved []SavedImage
		var warnings []map[string]any
		saved, warnings, err = SaveImageResults(ctx, results, outputDir, TimeoutSeconds*time.Second)
		if err == nil {
			warnings = append(warnings, ImageSizeWarnings(resolvedSize.Value, saved)...)
			if warnings == nil {
				warnings = []map[string]any{}
			}
			for index := range saved {
				if digest, hashErr := SHA256File(saved[index].Path); hashErr == nil {
					saved[index].SHA256 = digest
				}
			}
			stateFile, stateErr := WriteLastState(outputDir, saved, opts.Operation, model, resolvedSize.Value, scopeValue)
			if stateErr != nil {
				for _, image := range saved {
					_ = os.Remove(image.Path)
				}
				record.Status = "completed_unusable"
				record.UpdatedAt = nowISO()
				record.CompletedAt = record.UpdatedAt
				record.Failure = map[string]any{"error_kind": "output_save_error"}
				_ = atomicWriteJSON(recordPath, record)
				return nil, &ProviderError{Kind: "output_save_error", Message: "failed to write image state; the saved image was rolled back", Attempts: 1, ElapsedMS: time.Since(started).Milliseconds(), Details: map[string]any{}}
			}
			record.Images = recordImages(saved)
			record.Warnings = warnings
			record.LastStatePath = stateFile
			record.Status = "succeeded"
			record.UpdatedAt = nowISO()
			record.CompletedAt = record.UpdatedAt
			record.Failure = nil
			if writeErr := atomicWriteJSON(recordPath, record); writeErr != nil {
				for _, image := range saved {
					_ = os.Remove(image.Path)
				}
				_ = os.Remove(stateFile)
				return nil, &ProviderError{Kind: "output_save_error", Message: "failed to commit request state; the saved image was rolled back", Attempts: 1, ElapsedMS: time.Since(started).Milliseconds(), Details: map[string]any{"request_key": requestKey, "fingerprint": fingerprint, "status": "outcome_unknown", "may_have_been_billed": true}}
			}
			return generationResult(record, provider, endpoint, resolvedSize, inputImages, saved, warnings, false, "", stateFile), nil
		}
	}
	providerErr := asProviderError(err)
	if providerErr.Kind == "" {
		providerErr.Kind = "output_decode_error"
	}
	record.Status = "completed_unusable"
	record.UpdatedAt = nowISO()
	record.CompletedAt = record.UpdatedAt
	record.Failure = requestFailure(providerErr)
	_ = atomicWriteJSON(recordPath, record)
	providerErr.Details["request_key"] = requestKey
	providerErr.Details["fingerprint"] = fingerprint
	providerErr.Details["status"] = "completed_unusable"
	providerErr.Details["may_have_been_billed"] = true
	return nil, providerErr
}

func resolveInputImages(operation string, values []string, useLast bool, outputDir, scope string) ([]string, error) {
	if operation != "edit" {
		if len(values) > 0 || useLast {
			return nil, fmt.Errorf("generate operation does not accept input images")
		}
		return nil, nil
	}
	if useLast {
		path, err := ReadLastImage(outputDir, scope)
		if err != nil {
			return nil, err
		}
		return []string{path}, nil
	}
	if len(values) == 0 {
		return nil, fmt.Errorf("edit operation requires --last or at least one --image")
	}
	paths := make([]string, 0, len(values))
	for _, value := range values {
		abs, err := filepath.Abs(strings.TrimSpace(value))
		if err != nil {
			return nil, err
		}
		if err := ValidateInputFile(abs); err != nil {
			return nil, err
		}
		paths = append(paths, abs)
	}
	return paths, nil
}

func generationResult(record *RequestRecord, provider ProviderConfig, endpoint string, size ResolvedSize, inputImages []string, saved []SavedImage, warnings []map[string]any, reused bool, reusedFrom, stateFile string) map[string]any {
	if warnings == nil {
		warnings = []map[string]any{}
	}
	images := make([]map[string]any, 0, len(saved))
	for _, image := range saved {
		images = append(images, image.AsMap())
	}
	if stateFile == "" {
		stateFile = record.LastStatePath
	}
	attempts := 0
	if !reused {
		attempts = 1
	}
	return map[string]any{
		"ok":        true,
		"operation": record.Operation,
		"model":     record.Model,
		"provider": map[string]any{
			"id":                 provider.ProviderID,
			"name":               provider.Name,
			"credential_mode":    provider.CredentialMode,
			"credential_profile": provider.CredentialProfile,
		},
		"credential_profile": provider.CredentialProfile,
		"request": map[string]any{
			"transport":                "images",
			"endpoint":                 endpoint,
			"size":                     size.Value,
			"resolved_size":            size.AsMap(),
			"input_image_count":        len(inputImages),
			"request_key":              record.RequestKey,
			"fingerprint":              record.Fingerprint,
			"provider_timeout_seconds": TimeoutSeconds,
			"upstream_attempts":        attempts,
			"reused":                   reused,
			"reused_from_request_key":  nilIfEmpty(reusedFrom),
		},
		"images":     images,
		"state_path": stateFile,
		"warnings":   warnings,
	}
}

func nilIfEmpty(value string) any {
	if value == "" {
		return nil
	}
	return value
}

func asProviderError(err error) *ProviderError {
	if providerErr, ok := err.(*ProviderError); ok {
		if providerErr.Details == nil {
			providerErr.Details = map[string]any{}
		}
		return providerErr
	}
	return &ProviderError{Kind: "output_decode_error", Message: err.Error(), Attempts: 1, Details: map[string]any{}}
}
