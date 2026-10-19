package podotion

import (
	"bytes"
	"compress/flate"
	"context"
	"encoding/binary"
	"encoding/json"
	"hash/crc32"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeMinimalSkill(t *testing.T, root, marker string) {
	t.Helper()
	files := map[string]string{
		"SKILL.md":               "# Skill " + marker + "\n",
		"agents/openai.yaml":     "interface:\n  display_name: Podotion Image\n",
		"runtime.json":           `{"version":"` + marker + `"}` + "\n",
		"bin/podotion-image.cmd": "@echo off\n",
		"bin/podotion-image.ps1": "# launcher\n",
		"bin/podotion-image.sh":  "#!/bin/sh\n",
	}
	for relative, content := range files {
		path := filepath.Join(root, filepath.FromSlash(relative))
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

func makeStoredZip(files map[string][]byte) []byte {
	var body bytes.Buffer
	offsets := map[string]uint32{}
	for name, data := range files {
		offsets[name] = uint32(body.Len())
		writeLocalHeader(&body, name, data)
	}
	centralOffset := uint32(body.Len())
	for name, data := range files {
		writeCentralHeader(&body, name, data, offsets[name])
	}
	centralSize := uint32(body.Len()) - centralOffset
	var end [22]byte
	binary.LittleEndian.PutUint32(end[0:4], zipEOCDSignature)
	binary.LittleEndian.PutUint16(end[8:10], uint16(len(files)))
	binary.LittleEndian.PutUint16(end[10:12], uint16(len(files)))
	binary.LittleEndian.PutUint32(end[12:16], centralSize)
	binary.LittleEndian.PutUint32(end[16:20], centralOffset)
	body.Write(end[:])
	return body.Bytes()
}

func writeLocalHeader(buffer *bytes.Buffer, name string, data []byte) {
	var header [30]byte
	binary.LittleEndian.PutUint32(header[0:4], zipLocalSignature)
	binary.LittleEndian.PutUint16(header[4:6], 20)
	binary.LittleEndian.PutUint16(header[10:12], 0)
	binary.LittleEndian.PutUint32(header[14:18], crc32.ChecksumIEEE(data))
	binary.LittleEndian.PutUint32(header[18:22], uint32(len(data)))
	binary.LittleEndian.PutUint32(header[22:26], uint32(len(data)))
	binary.LittleEndian.PutUint16(header[26:28], uint16(len(name)))
	buffer.Write(header[:])
	buffer.WriteString(name)
	buffer.Write(data)
}

func writeCentralHeader(buffer *bytes.Buffer, name string, data []byte, offset uint32) {
	var header [46]byte
	binary.LittleEndian.PutUint32(header[0:4], zipCentralSignature)
	binary.LittleEndian.PutUint16(header[4:6], 20)
	binary.LittleEndian.PutUint16(header[6:8], 20)
	binary.LittleEndian.PutUint16(header[10:12], 0)
	binary.LittleEndian.PutUint32(header[16:20], crc32.ChecksumIEEE(data))
	binary.LittleEndian.PutUint32(header[20:24], uint32(len(data)))
	binary.LittleEndian.PutUint32(header[24:28], uint32(len(data)))
	binary.LittleEndian.PutUint16(header[28:30], uint16(len(name)))
	binary.LittleEndian.PutUint32(header[42:46], offset)
	buffer.Write(header[:])
	buffer.WriteString(name)
}

func archiveFiles(marker string) map[string][]byte {
	return map[string][]byte{
		"podotion-image-main/skills/podotion-image/SKILL.md":               []byte("# Skill " + marker + "\n"),
		"podotion-image-main/skills/podotion-image/agents/openai.yaml":     []byte("interface:\n  display_name: Podotion Image\n"),
		"podotion-image-main/skills/podotion-image/runtime.json":           []byte(`{"version":"` + marker + `"}` + "\n"),
		"podotion-image-main/skills/podotion-image/bin/podotion-image.cmd": []byte("@echo off\n"),
		"podotion-image-main/skills/podotion-image/bin/podotion-image.ps1": []byte("# launcher\n"),
		"podotion-image-main/skills/podotion-image/bin/podotion-image.sh":  []byte("#!/bin/sh\n"),
		"podotion-image-main/README.md":                                    []byte("ignored\n"),
	}
}

func TestUpdateSkillTransaction(t *testing.T) {
	codexHome := t.TempDir()
	target := ExpectedSkillRoot(codexHome)
	writeMinimalSkill(t, target, "old")
	archive := makeStoredZip(archiveFiles("new"))
	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("Content-Type", "application/zip")
		_, _ = writer.Write(archive)
	}))
	defer server.Close()
	originalURL := repositoryArchiveURL
	originalClient := lifecycleHTTPClient
	repositoryArchiveURL = server.URL + "/archive.zip"
	lifecycleHTTPClient = server.Client()
	defer func() {
		repositoryArchiveURL = originalURL
		lifecycleHTTPClient = originalClient
	}()

	dry, err := UpdateSkill(context.Background(), target, codexHome, true)
	if err != nil {
		t.Fatal(err)
	}
	if dry["would_update"] != true {
		t.Fatalf("dry run should report an update")
	}
	raw, _ := os.ReadFile(filepath.Join(target, "SKILL.md"))
	if !strings.Contains(string(raw), "old") {
		t.Fatalf("dry run modified the Skill")
	}

	result, err := UpdateSkill(context.Background(), target, codexHome, false)
	if err != nil {
		t.Fatal(err)
	}
	if result["updated"] != true {
		t.Fatalf("update did not report success: %v", result)
	}
	raw, _ = os.ReadFile(filepath.Join(target, "SKILL.md"))
	if !strings.Contains(string(raw), "new") {
		t.Fatalf("Skill was not updated: %s", raw)
	}
	if _, err := os.Stat(filepath.Join(target, MetadataFilename)); err != nil {
		t.Fatalf("metadata was not written: %v", err)
	}

	again, err := UpdateSkill(context.Background(), target, codexHome, false)
	if err != nil {
		t.Fatal(err)
	}
	if again["updated"] != false {
		t.Fatalf("identical update should be a no-op")
	}
}

func TestUpdateRejectsIncompleteArchive(t *testing.T) {
	codexHome := t.TempDir()
	target := ExpectedSkillRoot(codexHome)
	writeMinimalSkill(t, target, "old")
	archive := makeStoredZip(map[string][]byte{
		"podotion-image-main/skills/podotion-image/SKILL.md": []byte("incomplete\n"),
	})
	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		_, _ = writer.Write(archive)
	}))
	defer server.Close()
	originalURL := repositoryArchiveURL
	originalClient := lifecycleHTTPClient
	repositoryArchiveURL = server.URL + "/archive.zip"
	lifecycleHTTPClient = server.Client()
	defer func() {
		repositoryArchiveURL = originalURL
		lifecycleHTTPClient = originalClient
	}()
	if _, err := UpdateSkill(context.Background(), target, codexHome, false); err == nil {
		t.Fatal("expected incomplete archive to fail")
	}
	raw, _ := os.ReadFile(filepath.Join(target, "SKILL.md"))
	if !strings.Contains(string(raw), "old") {
		t.Fatalf("failed update damaged the previous installation")
	}
}

func TestUninstallSkillPreservesCredentials(t *testing.T) {
	codexHome := t.TempDir()
	target := ExpectedSkillRoot(codexHome)
	writeMinimalSkill(t, target, "installed")
	credential := CredentialPath(codexHome)
	if err := os.MkdirAll(filepath.Dir(credential), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(credential, []byte("base_url = \"https://ai.podotion.com/v1\"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := UninstallSkill(target, codexHome, false); err == nil {
		t.Fatal("uninstall must require confirmation")
	}
	if _, err := UninstallSkill(target, codexHome, true); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(target); !os.IsNotExist(err) {
		t.Fatalf("Skill directory still exists")
	}
	if _, err := os.Stat(credential); err != nil {
		t.Fatalf("credential file was not preserved: %v", err)
	}
}

func TestLegacyPluginInspectionAndMigration(t *testing.T) {
	home := t.TempDir()
	codexHome := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	source := filepath.Join(home, "plugins", SkillName)
	if err := os.MkdirAll(source, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(source, "plugin.json"), []byte("{}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	marketplace := filepath.Join(home, ".agents", "plugins", "marketplace.json")
	if err := os.MkdirAll(filepath.Dir(marketplace), 0o755); err != nil {
		t.Fatal(err)
	}
	payload := map[string]any{
		"name": LegacyMarketplaceName,
		"plugins": []any{
			map[string]any{"name": SkillName, "source": map[string]any{"source": LegacyMarketplaceSource, "path": LegacyMarketplaceRelPath}},
			map[string]any{"name": "other", "source": map[string]any{"source": "local", "path": "./plugins/other"}},
		},
	}
	raw, _ := json.Marshal(payload)
	if err := os.WriteFile(marketplace, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	inspection := inspectLegacyPlugin(home)
	if inspection["detected"] != true || inspection["safe_to_remove"] != true {
		t.Fatalf("unexpected inspection: %v", inspection)
	}
	result, err := UninstallLegacyPlugin(codexHome, true)
	if err != nil {
		t.Fatal(err)
	}
	if result["uninstalled"] != true {
		t.Fatalf("migration failed: %v", result)
	}
	if _, err := os.Stat(source); !os.IsNotExist(err) {
		t.Fatalf("legacy source still exists")
	}
	updated, err := readMarketplace(marketplace)
	if err != nil {
		t.Fatal(err)
	}
	plugins := updated["plugins"].([]any)
	if len(plugins) != 1 || plugins[0].(map[string]any)["name"] != "other" {
		t.Fatalf("unexpected plugins after migration: %v", plugins)
	}
}

func TestLegacyDifferentSourceIsUnsafe(t *testing.T) {
	home := t.TempDir()
	marketplace := filepath.Join(home, ".agents", "plugins", "marketplace.json")
	if err := os.MkdirAll(filepath.Dir(marketplace), 0o755); err != nil {
		t.Fatal(err)
	}
	payload := map[string]any{
		"name": LegacyMarketplaceName,
		"plugins": []any{
			map[string]any{"name": SkillName, "source": map[string]any{"source": "local", "path": "./plugins/other"}},
		},
	}
	raw, _ := json.Marshal(payload)
	if err := os.WriteFile(marketplace, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	inspection := inspectLegacyPlugin(home)
	if inspection["safe_to_remove"] != false {
		t.Fatalf("different-source registration must be unsafe: %v", inspection)
	}
}

func makeDeflatedZip(files map[string][]byte) []byte {
	var body bytes.Buffer
	offsets := map[string]uint32{}
	compressed := map[string][]byte{}
	for name, data := range files {
		var buffer bytes.Buffer
		writer, _ := flate.NewWriter(&buffer, flate.DefaultCompression)
		_, _ = writer.Write(data)
		_ = writer.Close()
		compressed[name] = buffer.Bytes()
		offsets[name] = uint32(body.Len())
		writeDeflatedLocalHeader(&body, name, data, compressed[name])
	}
	centralOffset := uint32(body.Len())
	for name, data := range files {
		writeDeflatedCentralHeader(&body, name, data, compressed[name], offsets[name])
	}
	centralSize := uint32(body.Len()) - centralOffset
	var end [22]byte
	binary.LittleEndian.PutUint32(end[0:4], zipEOCDSignature)
	binary.LittleEndian.PutUint16(end[8:10], uint16(len(files)))
	binary.LittleEndian.PutUint16(end[10:12], uint16(len(files)))
	binary.LittleEndian.PutUint32(end[12:16], centralSize)
	binary.LittleEndian.PutUint32(end[16:20], centralOffset)
	body.Write(end[:])
	return body.Bytes()
}

func writeDeflatedLocalHeader(buffer *bytes.Buffer, name string, data, compressed []byte) {
	var header [30]byte
	binary.LittleEndian.PutUint32(header[0:4], zipLocalSignature)
	binary.LittleEndian.PutUint16(header[4:6], 20)
	binary.LittleEndian.PutUint16(header[10:12], 8)
	binary.LittleEndian.PutUint32(header[14:18], crc32.ChecksumIEEE(data))
	binary.LittleEndian.PutUint32(header[18:22], uint32(len(compressed)))
	binary.LittleEndian.PutUint32(header[22:26], uint32(len(data)))
	binary.LittleEndian.PutUint16(header[26:28], uint16(len(name)))
	buffer.Write(header[:])
	buffer.WriteString(name)
	buffer.Write(compressed)
}

func writeDeflatedCentralHeader(buffer *bytes.Buffer, name string, data, compressed []byte, offset uint32) {
	var header [46]byte
	binary.LittleEndian.PutUint32(header[0:4], zipCentralSignature)
	binary.LittleEndian.PutUint16(header[4:6], 20)
	binary.LittleEndian.PutUint16(header[6:8], 20)
	binary.LittleEndian.PutUint16(header[10:12], 8)
	binary.LittleEndian.PutUint32(header[16:20], crc32.ChecksumIEEE(data))
	binary.LittleEndian.PutUint32(header[20:24], uint32(len(compressed)))
	binary.LittleEndian.PutUint32(header[24:28], uint32(len(data)))
	binary.LittleEndian.PutUint16(header[28:30], uint16(len(name)))
	binary.LittleEndian.PutUint32(header[42:46], offset)
	buffer.Write(header[:])
	buffer.WriteString(name)
}

func TestExtractSkillArchiveDeflated(t *testing.T) {
	archive := makeDeflatedZip(archiveFiles("deflated"))
	destination := t.TempDir() + "/skill"
	if err := extractSkillArchive(archive, destination); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(filepath.Join(destination, "SKILL.md"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(raw), "deflated") {
		t.Fatalf("deflated archive was not extracted correctly")
	}
}

func TestExtractSkillArchiveRejectsTraversal(t *testing.T) {
	files := archiveFiles("unsafe")
	files["podotion-image-main/skills/podotion-image/../../escape.txt"] = []byte("escape")
	archive := makeStoredZip(files)
	if err := extractSkillArchive(archive, t.TempDir()+"/skill"); err == nil {
		t.Fatal("expected path traversal to be rejected")
	}
}
