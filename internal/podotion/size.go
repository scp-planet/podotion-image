package podotion

import (
	"fmt"
	"math"
	"regexp"
	"strconv"
	"strings"
)

var exactSizePattern = regexp.MustCompile(`^([1-9][0-9]*)x([1-9][0-9]*)$`)

func NormalizeSize(value string) (string, error) {
	size := strings.ToLower(strings.TrimSpace(value))
	if size == "" || size == AutoSize {
		return size, nil
	}
	if _, ok := SupportedTiers[size]; ok {
		return size, nil
	}
	match := exactSizePattern.FindStringSubmatch(size)
	if match == nil {
		return "", fmt.Errorf("size must be auto, 1k, 2k, 4k, or WIDTHxHEIGHT")
	}
	width, _ := strconv.Atoi(match[1])
	height, _ := strconv.Atoi(match[2])
	if err := ValidateDimensions(width, height); err != nil {
		return "", err
	}
	return fmt.Sprintf("%dx%d", width, height), nil
}

func NormalizeRatio(value string) (string, error) {
	ratio := strings.TrimSpace(value)
	if ratio == "" {
		return "", nil
	}
	if _, ok := SupportedRatios[ratio]; !ok {
		options := make([]string, 0, len(SupportedRatios))
		for key := range SupportedRatios {
			options = append(options, key)
		}
		return "", fmt.Errorf("unsupported ratio %q; choose one of: %s", ratio, strings.Join(options, ", "))
	}
	return ratio, nil
}

func ValidateDimensions(width, height int) error {
	if width <= 0 || height <= 0 {
		return fmt.Errorf("dimensions must be positive integers")
	}
	pixels := width * height
	longEdge := max(width, height)
	shortEdge := min(width, height)
	if longEdge > MaxEdge {
		return fmt.Errorf("long edge %dpx exceeds %dpx", longEdge, MaxEdge)
	}
	if width%DimensionMultiple != 0 || height%DimensionMultiple != 0 {
		return fmt.Errorf("dimensions must be multiples of %dpx", DimensionMultiple)
	}
	if float64(longEdge)/float64(shortEdge) > 3 {
		return fmt.Errorf("long edge to short edge ratio exceeds 3:1")
	}
	if pixels < MinPixels || pixels > MaxPixels {
		return fmt.Errorf("pixel count %d is outside %d-%d", pixels, MinPixels, MaxPixels)
	}
	return nil
}

func ResolveSize(tier, ratio string) (ResolvedSize, error) {
	tier = strings.ToLower(strings.TrimSpace(tier))
	if _, ok := SupportedTiers[tier]; !ok {
		return ResolvedSize{}, fmt.Errorf("unsupported size tier %q", tier)
	}
	if _, ok := SupportedRatios[ratio]; !ok {
		return ResolvedSize{}, fmt.Errorf("unsupported ratio %q", ratio)
	}
	if tier == "4k" {
		dims := fourKDimensionsByRatio[ratio]
		if err := ValidateDimensions(dims[0], dims[1]); err != nil {
			return ResolvedSize{}, err
		}
		return ResolvedSize{
			Value:          fmt.Sprintf("%dx%d", dims[0], dims[1]),
			RequestedSize:  tier,
			RequestedTier:  tier,
			RequestedRatio: ratio,
			Width:          dims[0],
			Height:         dims[1],
		}, nil
	}

	ratioDims := SupportedRatios[ratio]
	widthRatio, heightRatio := ratioDims[0], ratioDims[1]
	targetLong := SupportedTiers[tier]
	ratioLong := max(widthRatio, heightRatio)
	ratioShort := min(widthRatio, heightRatio)
	if float64(ratioLong)/float64(ratioShort) > 3 {
		return ResolvedSize{}, fmt.Errorf("ratio %s exceeds the 3:1 long-edge limit", ratio)
	}
	unit := lcm(DimensionMultiple/gcd(widthRatio, DimensionMultiple), DimensionMultiple/gcd(heightRatio, DimensionMultiple))
	maxByEdge := MaxEdge / (ratioLong * unit)
	maxByTier := targetLong / (ratioLong * unit)
	maxByPixels := int(math.Floor(math.Sqrt(float64(MaxPixels) / float64(widthRatio*heightRatio*unit*unit))))
	scale := min(maxByEdge, maxByTier, maxByPixels)
	if scale < 1 {
		return ResolvedSize{}, fmt.Errorf("cannot map %s %s into a valid image size", tier, ratio)
	}
	width := widthRatio * unit * scale
	height := heightRatio * unit * scale
	if width*height < MinPixels {
		scale = int(math.Ceil(math.Sqrt(float64(MinPixels) / float64(widthRatio*heightRatio*unit*unit))))
		width = widthRatio * unit * scale
		height = heightRatio * unit * scale
	}
	if err := ValidateDimensions(width, height); err != nil {
		return ResolvedSize{}, err
	}
	return ResolvedSize{
		Value:          fmt.Sprintf("%dx%d", width, height),
		RequestedSize:  tier,
		RequestedTier:  tier,
		RequestedRatio: ratio,
		Width:          width,
		Height:         height,
	}, nil
}

func ResolveRequestSize(size, ratio string) (ResolvedSize, error) {
	normalizedSize, err := NormalizeSize(size)
	if err != nil {
		return ResolvedSize{}, err
	}
	normalizedRatio, err := NormalizeRatio(ratio)
	if err != nil {
		return ResolvedSize{}, err
	}
	if _, ok := SupportedTiers[normalizedSize]; ok {
		if normalizedRatio == "" {
			return ResolvedSize{}, fmt.Errorf("--ratio is required when --size is a tier")
		}
		return ResolveSize(normalizedSize, normalizedRatio)
	}
	if normalizedSize != "" {
		if normalizedRatio != "" {
			return ResolvedSize{}, fmt.Errorf("--ratio cannot be used with an exact WIDTHxHEIGHT size")
		}
		width, height, _ := ParseExactSize(normalizedSize)
		return ResolvedSize{
			Value:         normalizedSize,
			RequestedSize: normalizedSize,
			Width:         width,
			Height:        height,
		}, nil
	}
	if normalizedRatio != "" {
		return ResolveSize(DefaultTier, normalizedRatio)
	}
	return ResolvedSize{Value: AutoSize}, nil
}

func ParseExactSize(value string) (int, int, error) {
	match := exactSizePattern.FindStringSubmatch(strings.ToLower(strings.TrimSpace(value)))
	if match == nil {
		return 0, 0, fmt.Errorf("size must use WIDTHxHEIGHT")
	}
	width, _ := strconv.Atoi(match[1])
	height, _ := strconv.Atoi(match[2])
	return width, height, nil
}

func gcd(a, b int) int {
	for b != 0 {
		a, b = b, a%b
	}
	return a
}

func lcm(a, b int) int {
	return a / gcd(a, b) * b
}
