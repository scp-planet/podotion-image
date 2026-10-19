package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"unicode/utf8"

	"github.com/scp-planet/podotion-image/internal/podotion"
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

func run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	global := flag.NewFlagSet("podotion-image", flag.ContinueOnError)
	global.SetOutput(io.Discard)
	credentialFile := global.String("credential-file", "", "private provider.toml path")
	globalModel := global.String("model", "", "image model override")
	if err := global.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			printUsage(stdout)
			return 0
		}
		return emitError(stderr, err)
	}
	rest := global.Args()
	if len(rest) == 0 {
		printUsage(stdout)
		return 0
	}
	command := rest[0]
	commandArgs := rest[1:]
	switch command {
	case "help", "-h", "--help":
		printUsage(stdout)
		return 0
	case "version":
		return emitJSON(stdout, map[string]any{
			"ok":            true,
			"operation":     "version",
			"version":       podotion.BinaryVersion,
			"default_model": podotion.DefaultModel,
			"runtime":       "go",
		})
	case "generate":
		return runImageCommand(commandArgs, *credentialFile, *globalModel, "generate", stdin, stdout, stderr)
	case "edit":
		return runImageCommand(commandArgs, *credentialFile, *globalModel, "edit", stdin, stdout, stderr)
	case "doctor":
		return runDoctor(commandArgs, *credentialFile, *globalModel, stdout, stderr)
	case "sizes":
		return runSizes(commandArgs, *globalModel, stdout, stderr)
	case "config":
		return runConfig(commandArgs, *credentialFile, stdin, stdout, stderr)
	case "status":
		return runStatus(commandArgs, stdout, stderr)
	case "update":
		return runUpdate(commandArgs, stdout, stderr)
	case "uninstall":
		return runUninstall(commandArgs, stdout, stderr)
	case "uninstall-legacy-plugin":
		return runUninstallLegacy(commandArgs, stdout, stderr)
	case "request-status":
		return runRequestStatus(commandArgs, stdout, stderr)
	case "request-abandon":
		return runRequestAbandon(commandArgs, stdout, stderr)
	default:
		return emitError(stderr, fmt.Errorf("unknown command %q", command))
	}
}

type stringList []string

func (s *stringList) String() string { return strings.Join(*s, ",") }
func (s *stringList) Set(value string) error {
	*s = append(*s, value)
	return nil
}

func credentialOrDefault(value, fallback string) string {
	if strings.TrimSpace(value) != "" {
		return value
	}
	return fallback
}

func modelOrDefault(value, fallback string) string {
	if strings.TrimSpace(value) != "" {
		return value
	}
	return fallback
}

func runImageCommand(args []string, globalCredential, globalModel, operation string, stdin io.Reader, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet(operation, flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	prompt := fs.String("prompt", "", "image prompt")
	promptFile := fs.String("prompt-file", "", "UTF-8 prompt file, or - for stdin")
	size := fs.String("size", "", "1k, 2k, 4k, auto, or WIDTHxHEIGHT")
	ratio := fs.String("ratio", "", "aspect ratio")
	outputDir := fs.String("output-dir", "", "output directory")
	requestKey := fs.String("request-key", "", "stable request key")
	forceNew := fs.Bool("force-new", false, "create a new variant")
	stateScope := fs.String("state-scope", "", "stable task scope")
	credentialFile := fs.String("credential-file", "", "private provider.toml path")
	model := fs.String("model", "", "image model override")
	var images stringList
	useLast := false
	if operation == "edit" {
		fs.Var(&images, "image", "input image; repeat up to five times")
		fs.BoolVar(&useLast, "last", false, "use the last generated image")
	}
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			printImageUsage(stdout, operation)
			return 0
		}
		return emitError(stderr, err)
	}
	text, err := readPrompt(*prompt, *promptFile, stdin)
	if err != nil {
		return emitError(stderr, err)
	}
	if strings.TrimSpace(*requestKey) == "" {
		return emitError(stderr, fmt.Errorf("--request-key is required"))
	}
	if operation == "edit" {
		if useLast && len(images) > 0 {
			return emitError(stderr, fmt.Errorf("--last and --image are mutually exclusive"))
		}
		if !useLast && len(images) == 0 {
			return emitError(stderr, fmt.Errorf("edit requires --last or at least one --image"))
		}
	}
	result, err := podotion.RunGeneration(context.Background(), podotion.GenerateOptions{
		CredentialFile: credentialOrDefault(*credentialFile, globalCredential),
		Model:          modelOrDefault(*model, globalModel),
		Operation:      operation,
		Prompt:         text,
		Size:           *size,
		Ratio:          *ratio,
		OutputDir:      *outputDir,
		RequestKey:     *requestKey,
		ForceNew:       *forceNew,
		StateScope:     *stateScope,
		InputImages:    images,
		UseLast:        useLast,
	})
	if err != nil {
		return emitError(stderr, err)
	}
	return emitResult(stdout, result)
}

func readPrompt(value, file string, stdin io.Reader) (string, error) {
	if strings.TrimSpace(value) != "" && strings.TrimSpace(file) != "" {
		return "", fmt.Errorf("--prompt and --prompt-file are mutually exclusive")
	}
	var text string
	if strings.TrimSpace(file) != "" {
		raw, err := readLimitedInput(file, stdin)
		if err != nil {
			return "", err
		}
		text = string(raw)
	} else {
		text = value
	}
	text = strings.TrimPrefix(text, "\ufeff")
	if !utf8.ValidString(text) {
		return "", fmt.Errorf("prompt must be valid UTF-8")
	}
	text = strings.TrimSpace(text)
	if text == "" {
		return "", fmt.Errorf("prompt cannot be empty")
	}
	return text, nil
}

func readLimitedInput(path string, stdin io.Reader) ([]byte, error) {
	var reader io.Reader
	var handle *os.File
	if path == "-" {
		reader = stdin
	} else {
		opened, err := os.Open(path)
		if err != nil {
			if os.IsNotExist(err) {
				return nil, fmt.Errorf("prompt file not found: %s", path)
			}
			return nil, err
		}
		handle = opened
		defer handle.Close()
		reader = handle
	}
	data, err := io.ReadAll(io.LimitReader(reader, 1024*1024+1))
	if err != nil {
		return nil, err
	}
	if len(data) > 1024*1024 {
		return nil, fmt.Errorf("prompt exceeds the 1 MB safety limit")
	}
	return data, nil
}

func runDoctor(args []string, globalCredential, globalModel string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("doctor", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	imageProbe := fs.Bool("image-probe", false, "perform a billable image generation probe")
	credentialFile := fs.String("credential-file", "", "private provider.toml path")
	model := fs.String("model", "", "image model override")
	outputDir := fs.String("output-dir", "", "probe output directory")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			printUsage(stdout)
			return 0
		}
		return emitError(stderr, err)
	}
	result, err := podotion.RunDoctor(context.Background(), podotion.DoctorOptions{
		CredentialFile: credentialOrDefault(*credentialFile, globalCredential),
		Model:          modelOrDefault(*model, globalModel),
		ImageProbe:     *imageProbe,
		OutputDir:      *outputDir,
	})
	if err != nil {
		return emitError(stderr, err)
	}
	return emitResult(stdout, result)
}

func runSizes(args []string, globalModel string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("sizes", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	model := fs.String("model", "", "image model")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			printUsage(stdout)
			return 0
		}
		return emitError(stderr, err)
	}
	return emitResult(stdout, podotion.RunSizes(modelOrDefault(*model, globalModel)))
}
func runConfig(args []string, globalCredential string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) > 0 && args[0] == "set-model" {
		fs := flag.NewFlagSet("config set-model", flag.ContinueOnError)
		fs.SetOutput(io.Discard)
		credentialFile := fs.String("credential-file", "", "private provider.toml path")
		if err := fs.Parse(args[1:]); err != nil {
			if errors.Is(err, flag.ErrHelp) {
				printUsage(stdout)
				return 0
			}
			return emitError(stderr, err)
		}
		if fs.NArg() != 1 {
			return emitError(stderr, fmt.Errorf("config set-model requires exactly one model name"))
		}
		result, err := podotion.SetModel(credentialOrDefault(*credentialFile, globalCredential), fs.Arg(0))
		if err != nil {
			return emitError(stderr, err)
		}
		return emitResult(stdout, result)
	}

	fs := flag.NewFlagSet("config", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	check := fs.Bool("check", false, "validate the existing credential file locally")
	fromStdin := fs.Bool("stdin", false, "read credentials from stdin")
	inputFile := fs.String("input-file", "", "read credentials from a local UTF-8 file")
	force := fs.Bool("force", false, "replace an existing credential file")
	set4k := fs.Bool("set-4k", false, "preserve the default credential and set only the 4K credential")
	credentialFile := fs.String("credential-file", "", "private provider.toml path")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			printUsage(stdout)
			return 0
		}
		return emitError(stderr, err)
	}
	if fs.NArg() != 0 {
		return emitError(stderr, fmt.Errorf("unsupported config argument: %s", fs.Arg(0)))
	}
	if *check && (*fromStdin || *inputFile != "" || *force || *set4k) {
		return emitError(stderr, fmt.Errorf("--check cannot be combined with an input source, --force, or --set-4k"))
	}
	if *fromStdin && *inputFile != "" {
		return emitError(stderr, fmt.Errorf("--stdin and --input-file are mutually exclusive"))
	}
	path := credentialOrDefault(*credentialFile, globalCredential)
	if *check {
		result, err := podotion.ConfigCheck(path)
		if err != nil {
			return emitError(stderr, err)
		}
		return emitResult(stdout, result)
	}
	if !*fromStdin && *inputFile == "" {
		return emitError(stderr, fmt.Errorf("config requires --stdin or --input-file"))
	}
	if *set4k && !*force {
		return emitError(stderr, fmt.Errorf("--set-4k requires --force"))
	}
	target, err := podotion.ConfigPath(path)
	if err != nil {
		return emitError(stderr, err)
	}
	if *inputFile != "" {
		inputPath, absErr := filepath.Abs(*inputFile)
		if absErr != nil {
			return emitError(stderr, absErr)
		}
		if strings.EqualFold(filepath.Clean(inputPath), filepath.Clean(target)) {
			return emitError(stderr, fmt.Errorf("credential input file must be different from the destination"))
		}
	}
	var input string
	if *fromStdin {
		raw, readErr := io.ReadAll(io.LimitReader(stdin, 2*64*1024+4097))
		if readErr != nil {
			return emitError(stderr, readErr)
		}
		if len(raw) > 2*64*1024+4096 {
			return emitError(stderr, fmt.Errorf("credential input exceeds the safety limit"))
		}
		input = string(raw)
	} else {
		input, err = podotion.ReadCredentialFile(*inputFile)
		if err != nil {
			return emitError(stderr, err)
		}
	}
	if *set4k {
		defaultSecret, _, existingModel, readErr := podotion.ReadExistingConfig(target)
		if readErr != nil {
			return emitError(stderr, readErr)
		}
		_, fourKSecret, parseErr := podotion.ParseCredentialInput(input, true)
		if parseErr != nil {
			return emitError(stderr, parseErr)
		}
		if _, writeErr := podotion.WriteConfig(target, defaultSecret, fourKSecret, existingModel); writeErr != nil {
			return emitError(stderr, writeErr)
		}
		return emitResult(stdout, map[string]any{
			"ok":          true,
			"operation":   "config",
			"config_path": target,
			"base_url":    podotion.DirectBaseURL,
			"model":       existingModel,
			"credential_profiles": map[string]any{
				"default": true,
				"4k":      true,
			},
		})
	}
	if _, statErr := os.Stat(target); statErr == nil && !*force {
		return emitError(stderr, fmt.Errorf("credential file already exists: %s; use --force to replace it", target))
	}
	defaultSecret, fourKSecret, parseErr := podotion.ParseCredentialInput(input, false)
	if parseErr != nil {
		return emitError(stderr, parseErr)
	}
	model := podotion.DefaultModel
	if existing, loadErr := podotion.LoadConfig(target); loadErr == nil {
		model = existing.Model
	}
	if _, writeErr := podotion.WriteConfig(target, defaultSecret, fourKSecret, model); writeErr != nil {
		return emitError(stderr, writeErr)
	}
	return emitResult(stdout, map[string]any{
		"ok":          true,
		"operation":   "config",
		"config_path": target,
		"base_url":    podotion.DirectBaseURL,
		"model":       model,
		"credential_profiles": map[string]any{
			"default": true,
			"4k":      fourKSecret != "",
		},
	})
}

func runStatus(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("status", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	skillRoot := fs.String("skill-root", "", "installed Skill directory")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			printUsage(stdout)
			return 0
		}
		return emitError(stderr, err)
	}
	codexHome, err := podotion.ResolveCodexHome()
	if err != nil {
		return emitError(stderr, err)
	}
	root := podotion.RunningSkillRoot(codexHome)
	if strings.TrimSpace(*skillRoot) != "" {
		root, err = filepath.Abs(*skillRoot)
		if err != nil {
			return emitError(stderr, err)
		}
	}
	return emitResult(stdout, podotion.BuildStatus(root, codexHome))
}

func runUpdate(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("update", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	dryRun := fs.Bool("dry-run", false, "download and validate only")
	skillRoot := fs.String("skill-root", "", "installed Skill directory")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			printUsage(stdout)
			return 0
		}
		return emitError(stderr, err)
	}
	codexHome, err := podotion.ResolveCodexHome()
	if err != nil {
		return emitError(stderr, err)
	}
	root := podotion.RunningSkillRoot(codexHome)
	if strings.TrimSpace(*skillRoot) != "" {
		root, err = filepath.Abs(*skillRoot)
		if err != nil {
			return emitError(stderr, err)
		}
	}
	result, err := podotion.UpdateSkill(context.Background(), root, codexHome, *dryRun)
	if err != nil {
		return emitError(stderr, err)
	}
	return emitResult(stdout, result)
}

func runUninstall(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("uninstall", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	yes := fs.Bool("yes", false, "confirm Skill removal")
	skillRoot := fs.String("skill-root", "", "installed Skill directory")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			printUsage(stdout)
			return 0
		}
		return emitError(stderr, err)
	}
	codexHome, err := podotion.ResolveCodexHome()
	if err != nil {
		return emitError(stderr, err)
	}
	root := podotion.RunningSkillRoot(codexHome)
	if strings.TrimSpace(*skillRoot) != "" {
		root, err = filepath.Abs(*skillRoot)
		if err != nil {
			return emitError(stderr, err)
		}
	}
	result, err := podotion.UninstallSkill(root, codexHome, *yes)
	if err != nil {
		return emitError(stderr, err)
	}
	return emitResult(stdout, result)
}

func runUninstallLegacy(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("uninstall-legacy-plugin", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	yes := fs.Bool("yes", false, "confirm legacy Plugin removal")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			printUsage(stdout)
			return 0
		}
		return emitError(stderr, err)
	}
	codexHome, err := podotion.ResolveCodexHome()
	if err != nil {
		return emitError(stderr, err)
	}
	result, err := podotion.UninstallLegacyPlugin(codexHome, *yes)
	if err != nil {
		return emitError(stderr, err)
	}
	return emitResult(stdout, result)
}

func runRequestStatus(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("request-status", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	requestKey := fs.String("request-key", "", "request key")
	outputDir := fs.String("output-dir", "", "output directory")
	stateScope := fs.String("state-scope", "", "stable task scope")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			printUsage(stdout)
			return 0
		}
		return emitError(stderr, err)
	}
	if strings.TrimSpace(*requestKey) == "" {
		return emitError(stderr, fmt.Errorf("--request-key is required"))
	}
	dir, err := podotion.ResolveOutputDir(*outputDir)
	if err != nil {
		return emitError(stderr, err)
	}
	result, err := podotion.GetRequestStatus(dir, *requestKey, *stateScope)
	if err != nil {
		return emitError(stderr, err)
	}
	return emitResult(stdout, result)
}

func runRequestAbandon(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("request-abandon", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	requestKey := fs.String("request-key", "", "request key")
	outputDir := fs.String("output-dir", "", "output directory")
	stateScope := fs.String("state-scope", "", "stable task scope")
	acknowledged := fs.Bool("acknowledge-possible-charge", false, "acknowledge a possible charge")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			printUsage(stdout)
			return 0
		}
		return emitError(stderr, err)
	}
	if strings.TrimSpace(*requestKey) == "" {
		return emitError(stderr, fmt.Errorf("--request-key is required"))
	}
	dir, err := podotion.ResolveOutputDir(*outputDir)
	if err != nil {
		return emitError(stderr, err)
	}
	result, err := podotion.AbandonRequest(dir, *requestKey, *stateScope, *acknowledged)
	if err != nil {
		return emitError(stderr, err)
	}
	return emitResult(stdout, result)
}

func emitResult(stdout io.Writer, result map[string]any) int {
	if err := writeJSON(stdout, result); err != nil {
		return emitError(os.Stderr, err)
	}
	if ok, exists := result["ok"]; exists {
		if parsed, isBool := ok.(bool); isBool && !parsed {
			return 1
		}
	}
	return 0
}

func emitJSON(stdout io.Writer, result map[string]any) int {
	if err := writeJSON(stdout, result); err != nil {
		return emitError(os.Stderr, err)
	}
	return 0
}

func emitError(stderr io.Writer, err error) int {
	var payload map[string]any
	var providerErr *podotion.ProviderError
	if errors.As(err, &providerErr) {
		payload = providerErr.AsMap()
	} else {
		payload = map[string]any{
			"type":                "LocalError",
			"error_kind":          "local_error",
			"message":             podotion.RedactSecrets(err.Error()),
			"http_status":         nil,
			"request_id":          nil,
			"cf_ray":              nil,
			"elapsed_ms":          nil,
			"attempts":            0,
			"retry_after":         nil,
			"first_http_status":   nil,
			"first_error_message": nil,
			"details":             map[string]any{},
		}
	}
	if message, ok := payload["message"].(string); ok {
		payload["message"] = podotion.RedactSecrets(message)
	}
	_ = writeJSON(stderr, map[string]any{"ok": false, "error": payload})
	return 1
}

func writeJSON(writer io.Writer, value any) error {
	encoder := json.NewEncoder(writer)
	encoder.SetIndent("", "  ")
	encoder.SetEscapeHTML(false)
	return encoder.Encode(value)
}

func printUsage(writer io.Writer) {
	fmt.Fprint(writer, `Podotion Image native CLI

Usage:
  podotion-image [--credential-file PATH] [--model NAME] <command> [options]

Commands:
  generate            Generate one image from a prompt
  edit                Edit one or more images
  doctor              Check configuration and connectivity
  sizes               Print the supported size mapping
  config              Write credentials, check config, or set the default model
  status              Show the installed Skill status
  update              Update the Skill from GitHub without Git
  uninstall           Remove the installed Skill code
  uninstall-legacy-plugin  Migrate the old Plugin installation
  request-status      Inspect a durable request
  request-abandon     Acknowledge and abandon an uncertain request
  version             Print the runtime version

Model precedence: --model > provider.toml model > new-install default `+podotion.DefaultModel+`
Legacy provider.toml without model: `+podotion.LegacyDefaultModel+`
`)
}

func printImageUsage(writer io.Writer, operation string) {
	fmt.Fprintf(writer, `Usage: podotion-image %s --prompt TEXT --request-key KEY [options]

Options:
  --prompt TEXT            Image prompt
  --prompt-file PATH       UTF-8 prompt file, or - for stdin
  --size TIER|WxH          1k, 2k, 4k, auto, or exact WIDTHxHEIGHT
  --ratio RATIO            Aspect ratio when --size is a tier
  --output-dir PATH        Output directory (default ./PodotionImageOutput)
  --request-key KEY        Stable key for exactly one image action
  --force-new              Do not reuse a recent equivalent success
  --state-scope SCOPE      Stable task scope for recovery and --last
  --model NAME             Model override for this call
  --credential-file PATH   Private provider.toml path
`, operation)
	if operation == "edit" {
		fmt.Fprint(writer, `  --image PATH             Input image; repeat up to five times
  --last                   Edit the last generated image
`)
	}
}
