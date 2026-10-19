package podotion

import (
	"bytes"
	"compress/flate"
	"encoding/binary"
	"fmt"
	"hash/crc32"
	"io"
	"os"
	"path/filepath"
	"strings"
)

const (
	zipEOCDSignature    = 0x06054b50
	zipCentralSignature = 0x02014b50
	zipLocalSignature   = 0x04034b50
	maxExtractedBytes   = 200 * 1024 * 1024
)

func extractSkillArchive(data []byte, destination string) error {
	entries, err := readZipEntries(data)
	if err != nil {
		return err
	}
	prefixes := []string{"skills/" + SkillName + "/", "/skills/" + SkillName + "/"}
	found := false
	extractedBytes := int64(0)
	for _, entry := range entries {
		name := strings.ReplaceAll(entry.Name, "\\", "/")
		var relative string
		for _, prefix := range prefixes {
			if index := strings.Index(name, prefix); index >= 0 {
				relative = name[index+len(prefix):]
				break
			}
		}
		if relative == "" {
			continue
		}
		found = true
		cleaned := filepath.Clean(filepath.FromSlash(relative))
		if cleaned == "." || cleaned == ".." || strings.HasPrefix(cleaned, ".."+string(filepath.Separator)) || filepath.IsAbs(cleaned) {
			return fmt.Errorf("Skill update archive contains an unsafe path")
		}
		extractedBytes += int64(len(entry.Data))
		if extractedBytes > maxExtractedBytes {
			return fmt.Errorf("Skill update archive exceeds the extracted size limit")
		}
		target := filepath.Join(destination, cleaned)
		if entry.Directory {
			if err := os.MkdirAll(target, 0o755); err != nil {
				return err
			}
			continue
		}
		if entry.Mode&os.ModeSymlink != 0 {
			return fmt.Errorf("Skill update archive contains an unsupported symlink")
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		mode := entry.Mode.Perm()
		if mode == 0 {
			mode = 0o644
		}
		if strings.HasSuffix(relative, ".sh") && mode&0o111 == 0 {
			mode |= 0o755
		}
		if err := os.WriteFile(target, entry.Data, mode); err != nil {
			return err
		}
	}
	if !found {
		return fmt.Errorf("Skill update archive did not contain %s", "skills/"+SkillName)
	}
	if !hasRequiredSkillFiles(destination) {
		return fmt.Errorf("Skill update archive is incomplete")
	}
	return nil
}

type zipEntry struct {
	Name      string
	Data      []byte
	Directory bool
	Mode      os.FileMode
}

func readZipEntries(data []byte) ([]zipEntry, error) {
	eocd := findZipEOCD(data)
	if eocd < 0 {
		return nil, fmt.Errorf("Skill update archive is not a valid ZIP file")
	}
	if eocd+22 > len(data) {
		return nil, fmt.Errorf("Skill update archive is truncated")
	}
	entryCount := int(binary.LittleEndian.Uint16(data[eocd+10 : eocd+12]))
	centralOffset := int(binary.LittleEndian.Uint32(data[eocd+16 : eocd+20]))
	if entryCount == 0xFFFF || centralOffset == 0xFFFFFFFF {
		return nil, fmt.Errorf("Skill update archive uses unsupported ZIP64 format")
	}
	if centralOffset <= 0 || centralOffset >= len(data) {
		return nil, fmt.Errorf("Skill update archive has an invalid central directory")
	}
	entries := make([]zipEntry, 0, entryCount)
	offset := centralOffset
	for index := 0; index < entryCount; index++ {
		if offset+46 > len(data) || binary.LittleEndian.Uint32(data[offset:offset+4]) != zipCentralSignature {
			return nil, fmt.Errorf("Skill update archive has an invalid central directory entry")
		}
		flags := binary.LittleEndian.Uint16(data[offset+8 : offset+10])
		method := binary.LittleEndian.Uint16(data[offset+10 : offset+12])
		expectedCRC := binary.LittleEndian.Uint32(data[offset+16 : offset+20])
		compressedSize := int(binary.LittleEndian.Uint32(data[offset+20 : offset+24]))
		uncompressedSize := int(binary.LittleEndian.Uint32(data[offset+24 : offset+28]))
		nameLength := int(binary.LittleEndian.Uint16(data[offset+28 : offset+30]))
		extraLength := int(binary.LittleEndian.Uint16(data[offset+30 : offset+32]))
		commentLength := int(binary.LittleEndian.Uint16(data[offset+32 : offset+34]))
		externalAttributes := binary.LittleEndian.Uint32(data[offset+38 : offset+42])
		localOffset := int(binary.LittleEndian.Uint32(data[offset+42 : offset+46]))
		if offset+46+nameLength+extraLength+commentLength > len(data) {
			return nil, fmt.Errorf("Skill update archive has a truncated central directory")
		}
		name := string(data[offset+46 : offset+46+nameLength])
		if flags&0x1 != 0 {
			return nil, fmt.Errorf("Skill update archive contains an encrypted entry")
		}
		if localOffset+30 > len(data) || binary.LittleEndian.Uint32(data[localOffset:localOffset+4]) != zipLocalSignature {
			return nil, fmt.Errorf("Skill update archive has an invalid local file header")
		}
		localNameLength := int(binary.LittleEndian.Uint16(data[localOffset+26 : localOffset+28]))
		localExtraLength := int(binary.LittleEndian.Uint16(data[localOffset+28 : localOffset+30]))
		dataStart := localOffset + 30 + localNameLength + localExtraLength
		dataEnd := dataStart + compressedSize
		if dataStart < 0 || dataEnd > len(data) || dataEnd < dataStart {
			return nil, fmt.Errorf("Skill update archive has truncated file data")
		}
		payload, err := decompressZipEntry(data[dataStart:dataEnd], method, uncompressedSize)
		if err != nil {
			return nil, err
		}
		if crc32.ChecksumIEEE(payload) != expectedCRC {
			return nil, fmt.Errorf("Skill update archive contains a corrupt entry")
		}
		directory := strings.HasSuffix(name, "/")
		mode := os.FileMode(0o644)
		if unixMode := externalAttributes >> 16; unixMode != 0 {
			mode = os.FileMode(unixMode & 0o777)
		}
		if directory {
			mode = os.ModeDir | 0o755
		}
		entries = append(entries, zipEntry{Name: name, Data: payload, Directory: directory, Mode: mode})
		offset += 46 + nameLength + extraLength + commentLength
	}
	return entries, nil
}

func findZipEOCD(data []byte) int {
	minimum := 22
	if len(data) < minimum {
		return -1
	}
	start := len(data) - minimum
	stop := len(data) - 65557
	if stop < 0 {
		stop = 0
	}
	for index := start; index >= stop; index-- {
		if binary.LittleEndian.Uint32(data[index:index+4]) == zipEOCDSignature {
			return index
		}
	}
	return -1
}

func decompressZipEntry(data []byte, method uint16, expectedSize int) ([]byte, error) {
	if expectedSize < 0 || expectedSize > maxExtractedBytes {
		return nil, fmt.Errorf("Skill update archive contains an oversized entry")
	}
	switch method {
	case 0:
		if len(data) != expectedSize {
			return nil, fmt.Errorf("Skill update archive contains a corrupt stored entry")
		}
		return append([]byte(nil), data...), nil
	case 8:
		reader := flate.NewReader(bytes.NewReader(data))
		defer reader.Close()
		payload, err := io.ReadAll(io.LimitReader(reader, int64(expectedSize)+1))
		if err != nil {
			return nil, fmt.Errorf("Skill update archive contains a corrupt deflated entry")
		}
		if len(payload) != expectedSize {
			return nil, fmt.Errorf("Skill update archive contains an entry with an unexpected size")
		}
		return payload, nil
	default:
		return nil, fmt.Errorf("Skill update archive uses unsupported compression method %d", method)
	}
}
