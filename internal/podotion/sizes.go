package podotion

func RunSizes(model string) map[string]any {
	if model == "" {
		model = DefaultModel
	}
	sizes := map[string]any{}
	for _, tier := range []string{"1k", "2k", "4k"} {
		ratios := map[string]any{}
		for _, ratio := range []string{"1:1", "2:3", "3:2", "3:4", "4:3", "16:9", "9:16", "19:10", "10:19"} {
			resolved, err := ResolveSize(tier, ratio)
			if err == nil {
				ratios[ratio] = resolved.Value
			}
		}
		sizes[tier] = ratios
	}
	return map[string]any{
		"ok":            true,
		"operation":     "sizes",
		"model":         model,
		"default_model": DefaultModel,
		"sizes":         sizes,
	}
}
