package podotion

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"hash/crc32"
	"image"
	"image/color"
	"image/gif"
	"image/jpeg"
	"image/png"
	"io"
	"mime"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/image/bmp"
	"golang.org/x/image/tiff"
)

func fixPNGCRC(data []byte) { binary.BigEndian.PutUint32(data[29:33], crc32.ChecksumIEEE(data[12:29])) }

func encodedFixture(t *testing.T, format string) []byte {
	t.Helper()
	img := image.NewNRGBA(image.Rect(0, 0, 4, 3))
	img.SetNRGBA(1, 1, color.NRGBA{R: 200, G: 90, B: 10, A: 128})
	var buffer bytes.Buffer
	var err error
	switch format {
	case "jpeg":
		err = jpeg.Encode(&buffer, img, nil)
	case "png":
		err = png.Encode(&buffer, img)
	case "bmp":
		err = bmp.Encode(&buffer, img)
	case "tiff":
		err = tiff.Encode(&buffer, img, nil)
	case "gif":
		palette := color.Palette{color.NRGBA{}, color.NRGBA{R: 255, A: 255}, color.NRGBA{B: 255, A: 255}}
		first := image.NewPaletted(image.Rect(1, 1, 3, 2), palette)
		first.SetColorIndex(1, 1, 1)
		second := image.NewPaletted(image.Rect(0, 0, 4, 3), palette)
		second.SetColorIndex(0, 0, 2)
		err = gif.EncodeAll(&buffer, &gif.GIF{Image: []*image.Paletted{first, second}, Delay: []int{1, 1}, Config: image.Config{ColorModel: palette, Width: 4, Height: 3}})
	case "webp":
		data, decodeErr := base64.StdEncoding.DecodeString("UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA")
		if decodeErr != nil {
			t.Fatal(decodeErr)
		}
		return data
	default:
		t.Fatalf("unknown test fixture %s", format)
	}
	if err != nil {
		t.Fatal(err)
	}
	return buffer.Bytes()
}

func writeFixture(t *testing.T, path string, data []byte) string {
	t.Helper()
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestInputFormatsAndFirstFrame(t *testing.T) {
	for _, format := range []string{"jpeg", "png", "webp", "gif", "bmp", "tiff"} {
		t.Run(format, func(t *testing.T) {
			original := encodedFixture(t, format)
			prepared, err := prepareInputImage(original, 2)
			if err != nil {
				t.Fatal(err)
			}
			decoded, _, err := image.Decode(bytes.NewReader(prepared.Data))
			if err != nil {
				t.Fatal(err)
			}
			if format == "jpeg" || format == "png" || format == "webp" {
				if !bytes.Equal(original, prepared.Data) || prepared.MIME != "image/"+format {
					t.Fatalf("bytes/MIME changed: %+v", prepared)
				}
			} else if prepared.MIME != "image/png" || prepared.Suffix != ".png" {
				t.Fatal("conversion must really produce PNG")
			}
			if format == "gif" {
				if decoded.Bounds() != image.Rect(0, 0, 4, 3) {
					t.Fatalf("lost logical canvas: %v", decoded.Bounds())
				}
				_, _, _, outside := decoded.At(0, 0).RGBA()
				_, _, _, transparent := decoded.At(2, 1).RGBA()
				r, _, _, opaque := decoded.At(1, 1).RGBA()
				if outside != 0 || transparent != 0 || r != 65535 || opaque != 65535 {
					t.Fatal("first-frame offset/transparency not preserved")
				}
			}
			if format == "tiff" {
				_, _, _, alpha := decoded.At(1, 1).RGBA()
				if alpha != 128*257 {
					t.Fatalf("TIFF alpha lost: %d", alpha)
				}
			}
			if format == "gif" || format == "tiff" {
				if len(prepared.Warnings) != 1 || prepared.Warnings[0]["image_index"] != 2 {
					t.Fatal("missing structured conversion warning")
				}
			}
		})
	}
}

func TestMultipartTypesOrderAndExactBytes(t *testing.T) {
	for _, formats := range [][]string{{"jpeg"}, {"png"}, {"webp"}, {"jpeg", "png", "webp"}, {"gif", "bmp", "tiff"}} {
		t.Run(strings.Join(formats, "+"), func(t *testing.T) {
			dir := t.TempDir()
			paths := []string{}
			expected := []uploadImage{}
			for i, format := range formats {
				data := encodedFixture(t, format)
				// Intentionally misleading suffix, including WebP masquerading as JPEG.
				paths = append(paths, writeFixture(t, filepath.Join(dir, fmt.Sprintf("input-%d.jpg", i)), data))
				prepared, err := prepareInputImage(data, i+1)
				if err != nil {
					t.Fatal(err)
				}
				expected = append(expected, prepared)
			}
			server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				media, params, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
				if err != nil || media != "multipart/form-data" || params["boundary"] == "" {
					t.Errorf("wrong outer Content-Type: %s", r.Header.Get("Content-Type"))
					w.WriteHeader(400)
					return
				}
				reader, err := r.MultipartReader()
				if err != nil {
					t.Error(err)
					return
				}
				files := 0
				fields := map[string]string{}
				for {
					part, err := reader.NextPart()
					if err == io.EOF {
						break
					}
					if err != nil {
						t.Error(err)
						return
					}
					data, _ := io.ReadAll(part)
					if part.FileName() == "" {
						fields[part.FormName()] = string(data)
						continue
					}
					if files >= len(expected) {
						t.Error("too many files")
						return
					}
					want := expected[files]
					if part.FormName() != "image[]" || part.FileName() != fmt.Sprintf("image-%d%s", files+1, want.Suffix) || part.Header.Get("Content-Type") != want.MIME || !bytes.Equal(data, want.Data) {
						t.Errorf("file %d headers/bytes mismatch: %v", files, part.Header)
					}
					files++
				}
				if files != len(expected) || fields["n"] != "1" || fields["model"] != "test-model" || fields["prompt"] != "test prompt" {
					t.Errorf("multipart fields: %v files=%d", fields, files)
				}
				json.NewEncoder(w).Encode(map[string]any{"data": []any{map[string]any{"b64_json": base64.StdEncoding.EncodeToString(fakePNG(64, 64))}}})
			}))
			defer server.Close()
			old := providerTransport
			providerTransport = server.Client().Transport
			defer func() { providerTransport = old }()
			if _, err := PostImages(context.Background(), ProviderConfig{BaseURL: server.URL, Model: "test-model"}, "edit", "test prompt", paths, "auto"); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestInputRejections(t *testing.T) {
	animated := make([]byte, 30)
	copy(animated, "RIFF")
	binary.LittleEndian.PutUint32(animated[4:8], 22)
	copy(animated[8:], "WEBPVP8X")
	binary.LittleEndian.PutUint32(animated[16:20], 10)
	animated[20] = 2
	overPixels := encodedFixture(t, "png")
	binary.BigEndian.PutUint32(overPixels[16:20], 10000)
	binary.BigEndian.PutUint32(overPixels[20:24], 5001)
	// DecodeConfig requires a correct PNG IHDR CRC, even before pixel decoding.
	fixPNGCRC(overPixels)
	cases := map[string][]byte{"empty": nil, "corrupt-jpeg": {0xff, 0xd8, 0xff}, "truncated-png": encodedFixture(t, "png")[:25], "heic": []byte("\x00\x00\x00\x18ftypheic"), "avif": []byte("\x00\x00\x00\x18ftypavif"), "animated-webp": animated, "pixel-limit": overPixels, "byte-limit": make([]byte, MaxInputFileBytes+1)}
	for name, data := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := prepareInputImage(data, 1)
			if err == nil {
				t.Fatal("unsupported input accepted")
			}
			if name == "pixel-limit" && !strings.Contains(err.Error(), "pixel limit") {
				t.Fatalf("expected size check before decode: %v", err)
			}
		})
	}
}

func TestConvertedPNGSizeLimit(t *testing.T) {
	writer := limitedImageBuffer{limit: 16}
	if err := png.Encode(&writer, image.NewNRGBA(image.Rect(0, 0, 2, 2))); err == nil || !strings.Contains(err.Error(), "20 MB") {
		t.Fatalf("conversion did not enforce bounded output: %v", err)
	}
}

func TestMultipageTIFFUsesFirstPage(t *testing.T) {
	// Two little-endian RGBA pages: translucent red, then opaque blue.
	raw := make([]byte, 284)
	copy(raw, "II")
	binary.LittleEndian.PutUint16(raw[2:4], 42)
	binary.LittleEndian.PutUint32(raw[4:8], 8)
	for page := 0; page < 2; page++ {
		offset := 8 + page*126
		binary.LittleEndian.PutUint16(raw[offset:], 10)
		entries := [][4]uint32{{256, 4, 1, 1}, {257, 4, 1, 1}, {258, 3, 4, uint32(260 + page*8)}, {259, 3, 1, 1}, {262, 3, 1, 2}, {273, 4, 1, uint32(276 + page*4)}, {277, 3, 1, 4}, {278, 4, 1, 1}, {279, 4, 1, 4}, {338, 3, 1, 2}}
		for i, entry := range entries {
			p := raw[offset+2+i*12:]
			binary.LittleEndian.PutUint16(p, uint16(entry[0]))
			binary.LittleEndian.PutUint16(p[2:], uint16(entry[1]))
			binary.LittleEndian.PutUint32(p[4:], entry[2])
			binary.LittleEndian.PutUint32(p[8:], entry[3])
		}
		if page == 0 {
			binary.LittleEndian.PutUint32(raw[offset+122:], 134)
		}
		for i := 0; i < 4; i++ {
			binary.LittleEndian.PutUint16(raw[260+page*8+i*2:], 8)
		}
	}
	copy(raw[276:], []byte{255, 0, 0, 128, 0, 0, 255, 255})
	prepared, err := prepareInputImage(raw, 1)
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := png.Decode(bytes.NewReader(prepared.Data))
	if err != nil {
		t.Fatal(err)
	}
	pixel := color.NRGBAModel.Convert(decoded.At(0, 0)).(color.NRGBA)
	if pixel.R != 255 || pixel.B != 0 || pixel.A != 128 || len(prepared.Warnings) != 1 || prepared.Warnings[0]["code"] != "input_first_page" {
		t.Fatalf("wrong TIFF page: %v", pixel)
	}
}
