# Source record: YOLOX ONNX weights (person detection, COCO class 0)
- Project: Megvii-BaseDetection/YOLOX, release 0.1.1rc0. License: Apache-2.0 (https://github.com/Megvii-BaseDetection/YOLOX/blob/main/LICENSE)
- yolox_nano.onnx: 3659407 bytes, SHA-256 c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d, input 416x416
- yolox_tiny.onnx: 20219662 bytes, SHA-256 427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7, input 416x416
- Output: raw head [1, 3549, 85] (xywh offsets, sigmoid objectness, 80 sigmoid class scores); decoded in packages/vision/person_detector.py.
- Preprocessing (verified on frames by drawing boxes): BGR 0..255, no mean/std, top-left letterbox padded with 114.
- Weights are not committed (.gitignore); fetch with scripts/fetch_models.sh, which verifies the hashes. Runtime: OpenCV DNN, CPU, local only.
