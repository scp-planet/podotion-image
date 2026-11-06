package podotion

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"image"
	"image/color"
	"image/gif"
	"io"
	"mime"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func animatedOutputFixture(t *testing.T) []byte {
	t.Helper()
	palette := color.Palette{color.Transparent, color.NRGBA{R: 240, A: 255}, color.NRGBA{G: 200, A: 255}}
	first := image.NewPaletted(image.Rect(1, 1, 5, 4), palette)
	first.SetColorIndex(2, 2, 1)
	second := image.NewPaletted(image.Rect(0, 0, 7, 5), palette)
	second.SetColorIndex(4, 3, 2)
	animation := &gif.GIF{
		Image: []*image.Paletted{first, second}, Delay: []int{5, 12}, LoopCount: 3,
		Disposal: []byte{gif.DisposalNone, gif.DisposalBackground},
		Config:   image.Config{ColorModel: palette, Width: 7, Height: 5},
	}
	var output bytes.Buffer
	if err := gif.EncodeAll(&output, animation); err != nil {
		t.Fatal(err)
	}
	return output.Bytes()
}

func TestResolveOutputFormat(t *testing.T) {
	for _, test := range []struct{ operation, input, expected string }{
		{"generate", "", "png"}, {"edit", " PNG ", "png"},
		{"generate", "JPG", "jpeg"}, {"edit", "JPEG", "jpeg"},
		{"edit", "webp", "webp"}, {"generate", "GiF", "gif"},
	} {
		actual, err := ResolveOutputFormat(test.operation, test.input)
		if err != nil || actual != test.expected {
			t.Fatalf("%s %q: %s %v", test.operation, test.input, actual, err)
		}
	}
	for _, invalid := range []string{"svg", "avif", "image/jpeg", "png,gif"} {
		if _, err := ResolveOutputFormat("generate", invalid); err == nil {
			t.Fatalf("accepted unsupported output %q", invalid)
		}
	}
	if actual, err := ResolveOutputFormat("edit", "GIF"); err != nil || actual != "gif" {
		t.Fatalf("edit GIF output should be accepted: %q %v", actual, err)
	}
}

func TestOutputFormatPayloadAndMultipart(t *testing.T) {
	for _, format := range []string{"", "png", "JPG", "jpeg", "webp", "gif"} {
		expected, _ := ResolveOutputFormat("generate", format)
		raw, err := BuildImagesGenerationPayload("test-model", "prompt", "auto", format)
		if err != nil {
			t.Fatal(err)
		}
		var payload map[string]any
		json.Unmarshal(raw, &payload)
		if payload["output_format"] != expected || payload["n"] != float64(1) || payload["stream"] != nil {
			t.Fatalf("payload: %v", payload)
		}
		path := filepath.Join(t.TempDir(), "misnamed.gif")
		input := encodedFixture(t, "jpeg")
		writeFixture(t, path, input)
		body, contentType, err := BuildImagesEditMultipart("test-model", "prompt", []string{path}, "auto", format)
		if err != nil {
			t.Fatal(err)
		}
		mediaType, params, err := mime.ParseMediaType(contentType)
		if err != nil || mediaType != "multipart/form-data" || params["boundary"] == "" {
			t.Fatalf("outer Content-Type: %s", contentType)
		}
		reader := multipart.NewReader(bytes.NewReader(body), params["boundary"])
		fields := map[string]string{}
		files := 0
		for {
			part, err := reader.NextPart()
			if err == io.EOF {
				break
			}
			if err != nil {
				t.Fatal(err)
			}
			data, _ := io.ReadAll(part)
			if part.FileName() == "" {
				fields[part.FormName()] = string(data)
			} else {
				files++
				if part.Header.Get("Content-Type") != "image/jpeg" || part.FileName() != "image-1.jpg" || !bytes.Equal(data, input) {
					t.Fatal("output format changed reference MIME, extension or bytes")
				}
			}
		}
		if files != 1 || fields["output_format"] != expected || fields["n"] != "1" {
			t.Fatalf("fields: %v files=%d", fields, files)
		}
	}
}

func TestOutputBytesDimensionsAndAnimationPreserved(t *testing.T) {
	for _, format := range []string{"png", "jpeg", "webp", "gif"} {
		t.Run(format, func(t *testing.T) {
			data := encodedFixture(t, format)
			if format == "gif" {
				data = animatedOutputFixture(t)
			}
			config, _, err := image.DecodeConfig(bytes.NewReader(data))
			if err != nil {
				t.Fatal(err)
			}
			for _, kind := range []string{"base64", "data_url"} {
				value := base64.StdEncoding.EncodeToString(data)
				if kind == "data_url" {
					value = "data:image/" + format + ";base64," + value
				}
				saved, _, err := SaveImageResults(context.Background(), []ImageResult{{Kind: kind, Value: value}}, t.TempDir(), time.Second)
				if err != nil {
					t.Fatal(err)
				}
				actual, _ := os.ReadFile(saved[0].Path)
				suffix := "." + format
				if format == "jpeg" {
					suffix = ".jpg"
				}
				if !bytes.Equal(actual, data) || filepath.Ext(saved[0].Path) != suffix || saved[0].MimeType != "image/"+format || saved[0].Width == nil || *saved[0].Width != config.Width || *saved[0].Height != config.Height || saved[0].AsMap()["output_format"] != format {
					t.Fatalf("output altered: %+v", saved[0])
				}
				if format == "gif" {
					animation, err := gif.DecodeAll(bytes.NewReader(actual))
					if err != nil || len(animation.Image) != 2 || animation.LoopCount != 3 || !reflect.DeepEqual(animation.Delay, []int{5, 12}) || animation.Image[0].Bounds() != image.Rect(1, 1, 5, 4) || !reflect.DeepEqual(animation.Disposal, []byte{1, 2}) {
						t.Fatalf("animation lost: %v %v", animation, err)
					}
				}
			}
		})
	}
}

func TestFormatAwareReuseAndMismatch(t *testing.T) {
	var calls atomic.Int32
	batchTestServer(t, func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var payload map[string]any
		json.NewDecoder(r.Body).Decode(&payload)
		data := encodedFixture(t, payload["output_format"].(string))
		if payload["prompt"] == "mismatch" {
			data = encodedFixture(t, "png")
		}
		json.NewEncoder(w).Encode(map[string]any{"data": []any{map[string]any{"b64_json": base64.StdEncoding.EncodeToString(data)}}})
	})
	opts := GenerateOptions{Operation: "generate", Prompt: "same prompt", RequestKey: "format-request-png", StateScope: "formats", OutputDir: t.TempDir()}
	first, err := RunGeneration(context.Background(), opts)
	if err != nil || first["output_format"] != "png" {
		t.Fatalf("default: %v %v", first, err)
	}
	path, _ := requestRecordPath(opts.OutputDir, opts.RequestKey, opts.StateScope)
	raw, _ := os.ReadFile(path)
	var legacy map[string]any
	json.Unmarshal(raw, &legacy)
	delete(legacy, "output_format")
	atomicWriteJSON(path, legacy)
	resumed, err := RunGeneration(context.Background(), opts)
	if err != nil || resumed["request"].(map[string]any)["reused"] != true || resumed["output_format"] != "png" {
		t.Fatalf("legacy PNG recovery: %v %v", resumed, err)
	}
	opts.OutputFormat = "jpg"
	if _, err := RunGeneration(context.Background(), opts); err == nil || !strings.Contains(err.Error(), "different image inputs") {
		t.Fatalf("format change with same identity must conflict: %v", err)
	}
	opts.RequestKey = "format-request-jpg"
	second, err := RunGeneration(context.Background(), opts)
	if err != nil || second["output_format"] != "jpeg" || second["request"].(map[string]any)["reused"] != false {
		t.Fatalf("JPEG request reused PNG: %v %v", second, err)
	}
	opts.RequestKey, opts.OutputFormat = "format-request-jpeg-alias", "JPEG"
	alias, err := RunGeneration(context.Background(), opts)
	if err != nil || alias["request"].(map[string]any)["reused"] != true || calls.Load() != 2 {
		t.Fatalf("JPG/JPEG alias: %v %v calls=%d", alias, err, calls.Load())
	}
	opts.RequestKey, opts.OutputFormat = "format-request-gif", "gif"
	if _, err := RunGeneration(context.Background(), opts); err != nil || calls.Load() != 3 {
		t.Fatalf("GIF reused static output: %v calls=%d", err, calls.Load())
	}
	opts.RequestKey, opts.OutputFormat, opts.Prompt = "format-mismatch-jpeg", "jpeg", "mismatch"
	result, err := RunGeneration(context.Background(), opts)
	if err != nil || calls.Load() != 4 {
		t.Fatalf("mismatch must preserve usable result without retry: %v", err)
	}
	warnings := result["warnings"].([]map[string]any)
	images := result["images"].([]map[string]any)
	if len(warnings) != 1 || warnings[0]["code"] != "image_format_mismatch" || warnings[0]["requested_format"] != "jpeg" || warnings[0]["actual_format"] != "png" || images[0]["output_format"] != "png" || filepath.Ext(images[0]["path"].(string)) != ".png" {
		t.Fatalf("mismatch warning or true file format missing: %v", result)
	}
}

func TestGIFEditStillPreflightValidatesInputBeforePosts(t *testing.T) {
	var calls atomic.Int32
	batchTestServer(t, func(w http.ResponseWriter, r *http.Request) { calls.Add(1); successImage(w) })
	output := filepath.Join(t.TempDir(), "uncreated-output")
	_, err := RunGeneration(context.Background(), GenerateOptions{Operation: "edit", OutputFormat: "gif", OutputDir: output, RequestKey: "restricted-gif-edit", Prompt: "edit", InputImages: []string{"missing.jpg"}})
	if err == nil || !strings.Contains(err.Error(), "image file not found") {
		t.Fatalf("missing GIF edit input was not rejected locally: %v", err)
	}
	if _, err := os.Stat(output); !os.IsNotExist(err) {
		t.Fatal("invalid edit created output/state")
	}
	if _, err := PostImages(context.Background(), ProviderConfig{BaseURL: "https://example.com"}, "edit", "edit", []string{"missing"}, "auto", "gif"); err == nil || !strings.Contains(err.Error(), "image file not found") {
		t.Fatalf("direct GIF edit did not validate input: %v", err)
	}
	opts, manifest := batchOptions(t, 2)
	manifest.Jobs[1].Operation, manifest.Jobs[1].OutputFormat = "edit", "GIF"
	manifest.Jobs[1].Images = []string{"missing.jpg"}
	writeBatchManifest(t, opts, manifest)
	_, err = RunBatch(context.Background(), opts, nil)
	if err == nil || !strings.Contains(err.Error(), "image file not found") || calls.Load() != 0 {
		t.Fatalf("invalid GIF edit batch sent paid calls: %v calls=%d", err, calls.Load())
	}
	records, _ := iterRequestRecords(opts.OutputDir, opts.StateScope)
	if len(records) != 0 {
		t.Fatal("local restriction created potentially-billed records")
	}
}

func TestMixedOutputBatchConcurrentRecovery(t *testing.T) {
	arrived := make(chan struct{}, 4)
	release := make(chan struct{})
	releaseResponses := sync.OnceFunc(func() { close(release) })
	var calls atomic.Int32
	batchTestServer(t, func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var payload map[string]any
		json.NewDecoder(r.Body).Decode(&payload)
		data := encodedFixture(t, payload["output_format"].(string))
		if payload["output_format"] == "gif" {
			data = animatedOutputFixture(t)
		}
		arrived <- struct{}{}
		select {
		case <-release:
			json.NewEncoder(w).Encode(map[string]any{"data": []any{map[string]any{"b64_json": base64.StdEncoding.EncodeToString(data)}}})
		case <-r.Context().Done():
		}
	})
	opts, manifest := batchOptions(t, 4)
	formats := []string{"png", "jpeg", "webp", "gif"}
	for index := range manifest.Jobs {
		manifest.Jobs[index].OutputFormat = formats[index]
	}
	writeBatchManifest(t, opts, manifest)
	ctx, cancel := context.WithCancel(context.Background())
	type outcome struct {
		result map[string]any
		err    error
	}
	done := make(chan outcome, 1)
	finished := make(chan struct{})
	go func() {
		defer close(finished)
		result, err := RunBatch(ctx, opts, nil)
		done <- outcome{result, err}
	}()
	t.Cleanup(func() { cancel(); releaseResponses(); receiveWithin(t, finished) })
	for range formats {
		receiveWithin(t, arrived)
	}
	releaseResponses()
	completed := receiveWithin(t, done)
	if completed.err != nil {
		t.Fatal(completed.err)
	}
	paths := map[string]bool{}
	for index, event := range completed.result["jobs"].([]map[string]any) {
		result := event["result"].(map[string]any)
		path := result["images"].([]map[string]any)[0]["path"].(string)
		if result["output_format"] != formats[index] || paths[path] {
			t.Fatalf("format isolation: %v", result)
		}
		paths[path] = true
	}
	resumed, err := RunBatch(context.Background(), opts, nil)
	if err != nil || calls.Load() != 4 {
		t.Fatalf("resume submitted a new POST: %v calls=%d", err, calls.Load())
	}
	for _, event := range resumed["jobs"].([]map[string]any) {
		if event["result"].(map[string]any)["request"].(map[string]any)["reused"] != true {
			t.Fatal("successful output not reused")
		}
	}
}

func TestLegacyPNGBatchSnapshotRecovery(t *testing.T) {
	var calls atomic.Int32
	batchTestServer(t, func(w http.ResponseWriter, r *http.Request) { calls.Add(1); successImage(w) })
	opts, _ := batchOptions(t, 2)
	result, err := RunBatch(context.Background(), opts, nil)
	if err != nil {
		t.Fatal(err)
	}
	path := result["snapshot_path"].(string)
	raw, _ := os.ReadFile(path)
	var snapshot map[string]any
	json.Unmarshal(raw, &snapshot)
	for _, item := range snapshot["jobs"].([]any) {
		job := item.(map[string]any)
		delete(job, "output_format")
		recordPath, _ := requestRecordPath(opts.OutputDir, job["request_key"].(string), opts.StateScope)
		recordRaw, _ := os.ReadFile(recordPath)
		var record map[string]any
		json.Unmarshal(recordRaw, &record)
		delete(record, "output_format")
		atomicWriteJSON(recordPath, record)
	}
	atomicWriteJSON(path, snapshot)
	resumed, err := RunBatch(context.Background(), opts, nil)
	if err != nil || calls.Load() != 2 || resumed["succeeded"] != 2 {
		t.Fatalf("legacy snapshot incompatible: %v calls=%d", err, calls.Load())
	}
}

func TestGIFGenerationProviderRejectionRemainsUpstreamError(t *testing.T) {
	var calls atomic.Int32
	batchTestServer(t, func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var payload map[string]any
		json.NewDecoder(r.Body).Decode(&payload)
		if payload["output_format"] != "gif" || payload["model"] != "test-model" {
			t.Errorf("request changed format or model: %v", payload)
		}
		w.WriteHeader(http.StatusBadRequest)
		json.NewEncoder(w).Encode(map[string]any{"error": map[string]any{"message": "GIF output is not enabled for this request."}})
	})
	opts := GenerateOptions{Operation: "generate", OutputFormat: "gif", Prompt: "looping animation", RequestKey: "provider-gif-disabled", StateScope: "formats", OutputDir: t.TempDir()}
	_, err := RunGeneration(context.Background(), opts)
	var providerErr *ProviderError
	if !errors.As(err, &providerErr) || providerErr.Kind != "upstream_error" || providerErr.HTTPStatus == nil || *providerErr.HTTPStatus != 400 || providerErr.Attempts != 1 || strings.Contains(providerErr.Message, "官方未支持") {
		t.Fatalf("upstream restriction misrepresented: %v", err)
	}
	path, _ := requestRecordPath(opts.OutputDir, opts.RequestKey, opts.StateScope)
	record, err := readRequestRecord(path)
	if err != nil || record.Status != "failed_definitive" || record.OutputFormat != "gif" {
		t.Fatalf("provider failure not durably recorded: %v %v", record, err)
	}
	_, err = RunGeneration(context.Background(), opts)
	if !errors.As(err, &providerErr) || providerErr.Kind != "request_failed_definitive" || calls.Load() != 1 {
		t.Fatalf("terminal GIF request retried: %v calls=%d", err, calls.Load())
	}
}

func TestGIFResultURLPreservesAnimation(t *testing.T) {
	data := animatedOutputFixture(t)
	batchTestServer(t, func(w http.ResponseWriter, r *http.Request) {
		if !strings.Contains(r.Header.Get("Accept"), "image/gif") || r.Header.Get("Authorization") != "" {
			t.Error("download must accept GIF without forwarding credentials")
		}
		w.Header().Set("Content-Type", "image/png")
		w.Write(data)
	})
	oldValidator := validateDownloadURL
	validateDownloadURL = func(string) error { return nil }
	t.Cleanup(func() { validateDownloadURL = oldValidator })
	provider, _ := loadConfig("")
	saved, _, err := SaveImageResults(context.Background(), []ImageResult{{Kind: "url", Value: provider.BaseURL + "/misnamed.png"}}, t.TempDir(), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	actual, _ := os.ReadFile(saved[0].Path)
	if !bytes.Equal(actual, data) || saved[0].MimeType != "image/gif" || filepath.Ext(saved[0].Path) != ".gif" {
		t.Fatal("URL extension or header overrode actual GIF bytes")
	}
}
