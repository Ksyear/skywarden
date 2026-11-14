# Source record: sky / person segmentation weights (static-map keep masks)
- Model: Realcat/skywater_seg (Hugging Face), revision ec20751. License on the public model card: MIT.
- skywater_fp32.onnx: 99310780 bytes, SHA-256 e4e9a6927c2d910c3243f86e392b18da715b41c03e6e6f41672f8f6b8eaa71b5 (checked by packages/mapping/static_masks.py before use).
- Input 384x384 RGB, ImageNet mean/std; output logits [1, 4, 384, 384]; class 1 = sky, class 3 = person are excluded from the map.
- Use: keep masks for dense matching (packages/mapping/static_masks.py) and the sky/person measures in packages/mapping/static_eval.py. Runtime: OpenCV DNN, CPU, local only; no frames leave the machine.
- Weights are not committed (.gitignore covers data/models/**/*.onnx).
