package podotion

// Input decoding is deliberately separate from DetectImageType, which recognizes
// provider output without changing the existing response protocol.
import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"image"
	"image/draw"
	_ "image/gif"
	_ "image/jpeg"
	"image/png"
	"io"
	"os"

	_ "golang.org/x/image/bmp"
	_ "golang.org/x/image/tiff"
	_ "golang.org/x/image/webp"
)

const MaxInputPixels = 50_000_000

type uploadImage struct {
	Data     []byte
	MIME     string
	Suffix   string
	Original InputImageMeta
	Warnings []map[string]any
}

func prepareInputImages(paths []string) ([]uploadImage, error) {
	if len(paths) == 0 || len(paths) > MaxInputImages {
		return nil, fmt.Errorf("edit requires 1-%d input images", MaxInputImages)
	}
	images := make([]uploadImage, 0, len(paths))
	for index, path := range paths {
		if err := ValidateInputFile(path); err != nil {
			return nil, err
		}
		handle, err := os.Open(path)
		if err != nil {
			return nil, fmt.Errorf("cannot read input image %d: %w", index+1, err)
		}
		data, err := io.ReadAll(io.LimitReader(handle, MaxInputFileBytes+1))
		handle.Close()
		if err != nil {
			return nil, err
		}
		prepared, err := prepareInputImage(data, index+1)
		if err != nil {
			return nil, fmt.Errorf("input image %d: %w", index+1, err)
		}
		images = append(images, prepared)
	}
	return images, nil
}

func prepareInputImage(data []byte, index int) (uploadImage, error) {
	var out uploadImage
	if len(data) == 0 || len(data) > MaxInputFileBytes {
		return out, fmt.Errorf("input must contain 1-%d bytes (20 MB maximum)", MaxInputFileBytes)
	}
	out.Original = InputImageMeta{SHA256: fmt.Sprintf("%x", sha256.Sum256(data)), Bytes: int64(len(data))}
	if len(data) >= 12 && string(data[:4]) == "RIFF" && string(data[8:12]) == "WEBP" {
		if err := validateStaticWebP(data); err != nil {
			return out, err
		}
	}
	config, format, err := image.DecodeConfig(bytes.NewReader(data))
	if err != nil {
		return out, fmt.Errorf("unsupported or corrupt image; supported inputs: JPEG, PNG, static WebP, GIF, BMP, TIFF: %w", err)
	}
	switch format {
	case "jpeg", "png", "webp", "gif", "bmp", "tiff":
	default:
		return out, fmt.Errorf("unsupported image format %q", format)
	}
	if config.Width <= 0 || config.Height <= 0 || int64(config.Width)*int64(config.Height) > MaxInputPixels {
		return out, fmt.Errorf("decoded image exceeds the %d pixel limit", MaxInputPixels)
	}
	decoded, decodedFormat, err := image.Decode(bytes.NewReader(data))
	if err != nil || decodedFormat != format {
		return out, fmt.Errorf("corrupt or unsupported %s variant: %v", format, err)
	}
	bounds := decoded.Bounds()
	if int64(bounds.Dx())*int64(bounds.Dy()) > MaxInputPixels {
		return out, fmt.Errorf("decoded image exceeds the pixel limit")
	}
	switch format {
	case "jpeg":
		out.MIME, out.Suffix = "image/jpeg", ".jpg"
	case "png":
		out.MIME, out.Suffix = "image/png", ".png"
	case "webp":
		out.MIME, out.Suffix = "image/webp", ".webp"
	default:
		if format == "gif" {
			// gif.Decode reads only the first frame. Its bounds may be offset inside
			// the logical screen; the remaining canvas stays transparent.
			canvas := image.NewNRGBA(image.Rect(0, 0, config.Width, config.Height))
			draw.Draw(canvas, bounds.Intersect(canvas.Bounds()), decoded, bounds.Min, draw.Src)
			decoded = canvas
			out.Warnings = append(out.Warnings, map[string]any{"code": "input_first_frame", "image_index": index, "input_format": "gif", "uploaded_format": "png", "message": "GIF: only the first frame on its logical canvas is used; animation is not uploaded"})
		}
		if format == "tiff" {
			out.Warnings = append(out.Warnings, map[string]any{"code": "input_first_page", "image_index": index, "input_format": "tiff", "uploaded_format": "png", "message": "TIFF: only the first page is used"})
		}
		encoded := limitedImageBuffer{limit: MaxInputFileBytes}
		if err := png.Encode(&encoded, decoded); err != nil {
			return out, fmt.Errorf("cannot convert %s to PNG: %w", format, err)
		}
		data = encoded.Bytes()
		out.MIME, out.Suffix = "image/png", ".png"
	}
	if len(data) > MaxInputFileBytes {
		return out, fmt.Errorf("converted image exceeds the 20 MB limit")
	}
	out.Data = data
	return out, nil
}

type limitedImageBuffer struct {
	bytes.Buffer
	limit int
}

func (w *limitedImageBuffer) Write(data []byte) (int, error) {
	if len(data) > w.limit-w.Len() {
		return 0, fmt.Errorf("converted image exceeds the 20 MB limit")
	}
	return w.Buffer.Write(data)
}

func validateStaticWebP(data []byte) error {
	if uint64(binary.LittleEndian.Uint32(data[4:8]))+8 != uint64(len(data)) {
		return fmt.Errorf("corrupt WebP RIFF length")
	}
	for offset := 12; offset < len(data); {
		if len(data)-offset < 8 {
			return fmt.Errorf("corrupt WebP chunk")
		}
		kind := string(data[offset : offset+4])
		size := uint64(binary.LittleEndian.Uint32(data[offset+4 : offset+8]))
		end := uint64(offset) + 8 + size
		if end > uint64(len(data)) {
			return fmt.Errorf("corrupt WebP chunk size")
		}
		if kind == "ANIM" || kind == "ANMF" || (kind == "VP8X" && size > 0 && data[offset+8]&2 != 0) {
			return fmt.Errorf("animated WebP is not supported; supply a static frame")
		}
		offset = int(end + size%2)
		if offset > len(data) {
			return fmt.Errorf("corrupt WebP padding")
		}
	}
	return nil
}
